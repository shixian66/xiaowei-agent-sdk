# 小维：当前交接

> 更新：2026-09-29，Asia/Shanghai。本文记录当前事实；目标设计见 [ARCHITECTURE.md](ARCHITECTURE.md)，阶段路线见 [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)。

## 1. 用户已经确定的方向

- 以 M5 为仓库历史基线，未来产品以 OpenAI Agents SDK 为核心，完整重写五份根文档。
- 按 SDK 原生方式从产品需求出发设计，允许从零开始。旧代码能低成本复用才复用，不能复用就放弃；不要求保留旧架构。
- 用户提出把“数据库查询助手”与“慢查询诊断”设计为一个场景；产品据此统一为 StarRocks 数据库助手。
- 首版同时支持飞书与 Web 对话，并明确要求最小化。
- 用户已确认 MCP 定位：首版有最小通用 MCP Client Integration；StarRocks 先走本地受治理工具；首版不自建业务 MCP Server。
- 按资深 Python 架构师与 Agent 开发专家标准逐步交付，最终进行实际环境验证。

本版移除了上一稿“必须保留 PostgreSQL、Worker、TaskStore、CAS/fencing 底座”的前提。旧 S0–S6 迁移路线被 P0–P3 产品路线替代，上一稿复用表不再是实施约束。

## 2. 架构审查修订

七项治理建议及后续 MCP 定位已经纳入架构；用户以“可以，就这样定”确认讨论后的方向。最高原则为：

**OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**

- 会话架构依赖 SDK Session；SQLiteSession 仅为首版实现，不新增自研 Session 抽象。
- RunContext 收紧到身份、Target Scope、Tool Scope、预算及 Evidence 标识/元数据；凭据、连接、客户端和间接服务引用排除。这是小维约定，SDK 本身允许本地依赖且不会自动发送 context 给模型。
- 所有本地 function tools 经过薄的 Governed Tool Layer，再调用 StarRocks Adapter；MCP 调用发送前复用治理核心，结果进入模型前过滤，不另建 Agent Loop。
- 原始、模型、会话和渠道数据分别限额；Session 通过公开接口的薄策略包装在存储前和回放前过滤，不能假定 SQLiteSession 自动完成数据治理。
- output_type 后仍由代码验证 Evidence；覆盖最终回答持久化、发送、历史读取和重发，验证存在、归属及当前权限。证据真实不等于推断一定正确。
- 动态工具列表提前排除不支持/无权限能力；执行时仍复核具体参数、资源与最新权限。
- 生产默认关闭 tracing 与 trace 外发；真实数据 trace 只能显式限范围开启，关闭敏感内容标志不等于关闭 exporter。

MCP 是标准化工具接入机制，不能替代业务授权。外部 Server 独立负责服务端鉴权和执行约束；客户端检查不能证明远端内部已经执行本地 SQLGuard。配置注册仅适用于已有参数/结果/风险策略映射；未知或写工具不开放，空 MCP 配置不影响本地功能。

方向确认不代表代码实施、实战验证或产品验收。

## 3. 当前书面设计与实施计划

当前设计：一个 StarRocks Agent、SDK Runner/function tools/Session、Governed Tool Layer 与 StarRocks Adapter、最小 MCP Client Integration、一个 Python 进程，同一应用服务供 Web 与飞书调用。首版 Session 存储采用 SQLiteSession，应用记录先用 SQLite。

最小范围：本机 Web、飞书获准用户单聊、一个 StarRocks 连接、两端独立会话；查询及普通 EXPLAIN 为必交，已有审计源与 Profile 为环境增强项。新应用建议独立 `src/xiaowei/` 包，不装配旧 Runtime。

已编写 [P1-A：SDK 与治理执行核心实施计划](docs/superpowers/plans/2026-09-29-p1a-sdk-governed-core.md)，等待用户审阅计划和选择执行方式；产品代码尚未实施。此片固定先验证 Streamable HTTP，仅使用合成数据与临时 loopback MCP fixture，不交付业务 MCP Server。

P1-A 是可独立验证的内部核心，包含真实 SDK Runner、工具治理、Evidence、Session 数据策略与 MCP 协议验证。P1-B 接真实 StarRocks 和双入口并完成唯一主入口切换；P2 诊断、P3 实战继续按路线推进。不能用内部核心完成代替首版用户产品完成。

## 4. 当前仓库事实

| 项目 | 已核对事实 |
| --- | --- |
| 仓库 | [shixian66/xiaowei-agent-sdk](https://github.com/shixian66/xiaowei-agent-sdk) |
| 本地目录 | `/Users/kloenguyen/Desktop/agent-SDK` |
| 本轮源码基线 / 拉取时 main | `372c381f44ecfa1fa53961f137d0058033cbd805` |
| 基线提交标题 | `docs(m5): record postgres and compose evidence` |
| 工作分支 | `claude/sdk-core-docs` |
| 本次审查起点 | `1b65691f55b66f63073bc53635340b3699f4f02b`，七项治理建议的本地文档提交 |
| 本轮变更 | 五份根文档与新增 P1-A 实施计划，共六份 Markdown 文件 |
| 新产品实现 | 尚未实现；依赖中没有 `openai-agents`，没有新的双入口 SDK 产品 |
| 当前源码 | 旧 `xiaowei_agent` 包，规则解释器、确定性 Resolver/PlanCompiler/Runner 与 fake 慢查询场景 |
| 其他文件 | 业务代码、测试、依赖锁文件、CI 与 Compose 未修改 |
| 外部操作 | 本轮未运行真实模型、StarRocks 或飞书，未 push、部署、合并或归档 |

M5 是历史起点，历史验收不证明新 SDK 产品可用。保留旧文件仅是当前尚未开始源码替换，不表示新产品将复用全部旧实现。

## 5. 五份文档的作用

- [AGENTS.md](AGENTS.md)：从零设计、SDK 优先、Python 质量、按实际风险验证的工作规则。
- [ARCHITECTURE.md](ARCHITECTURE.md)：查询与诊断统一产品、双入口、原生 SDK、最小存储和执行边界。
- 本文：已确认事项、计划状态、真实代码状态和证据。
- [README.md](README.md)：新产品介绍与必要环境，明确尚无经验证的新启动命令。
- [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)：P0 设计、P1 双入口查询与 MCP 基础、P2 诊断、P3 实战试用的交付顺序。

旧 ADR、里程碑和旧启动命令只说明历史系统；当前新方向以这五份文档及用户更新指令为准。历史材料不要求逐条迁移或重新批准才能放弃旧框架。

## 6. 外部依据与版本限制

已核对 OpenAI 官方 SDK、Agent definitions、running agents、guardrails/approvals、MCP integration/observability 及 Session memory cookbook。官方文档明确本地 context 与模型上下文不同、output_type 用于结构化输出、tracing 默认可能开启；Session 的公开读写接口可承担应用数据策略，具体包装与 SQLiteSession 行为仍需锁版后实测。MCP 原生接入的治理扩展点、HTTP 接收限额及结果过滤也列入首个实施任务，不能假定 function tool guardrails 自动覆盖 MCP。

已核对 StarRocks 普通 EXPLAIN、EXPLAIN ANALYZE 和获取 Profile 文档：分析计划与实际执行不同，Profile 获取受环境影响。也核对了飞书官方 Python SDK 文档，其 Channel API 存在包迁移；实施时需验证发布包，不能把在线示例当成已安装可用接口。

来源链接集中在 [ARCHITECTURE.md](ARCHITECTURE.md)。尚未安装锁定 SDK、模型、数据库驱动、SQL 解析库或飞书包，也未核对目标 StarRocks 版本与权限；不能宣称依赖兼容性已经通过。

## 7. 验证记录与限制

本轮 MCP 定位与实施计划修订后执行：

- `git diff --check`：exit 0；变更范围为五份根文档与新增 P1-A 实施计划。
- 六份文档的 28 个本地链接、Markdown 代码围栏、占位标记及最高原则检查：0 个错误。
- `python -m pytest tests/security/test_docs_command_consistency.py tests/contract/test_doc_fact_binding.py -q`：5 passed，exit 0。

验证使用原项目现有虚拟环境 `/Users/kloenguyen/Desktop/agent/.venv/bin/python`，显式设置本仓库 `src` 为 `PYTHONPATH`；未安装新依赖。该结果只用于选中文档检查，不证明新工程锁定环境或新产品运行通过。

本轮自检范围为 MCP 首版定位、调用前/结果后治理、四种数据投影、Session 与 Evidence 边界，以及 P1-A/P1-B 交付范围。文档测试覆盖命令与部分旧事实绑定，不验证新架构语义；尚无独立审查、SDK 实现测试、全量产品测试、正式浏览器、真实模型、真实 StarRocks 或飞书运行证据。

## 8. 下一步

审阅 P1-A 的五项任务并确定执行方式后，从锁定 SDK、验证公开 Session/MCP 扩展点开始实施。MCP 方向已确定，不重复开启方向评审；后续发现 SDK 约束时记录具体证据，调整实现方式。

进入真实联调时需要确认：可用模型与预算、允许外发的数据、StarRocks 版本和获准只读范围、飞书应用及允许的单聊身份。凭据通过本机安全配置引用，不能粘贴到对话或文档。上述环境信息不阻塞设计和独立离线开发，但对应实战退出条件必须保留。
