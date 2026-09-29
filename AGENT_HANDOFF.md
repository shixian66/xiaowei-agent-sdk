# 小维：当前交接

> 更新：2026-09-29，Asia/Shanghai。本文记录当前事实；目标设计见 [ARCHITECTURE.md](ARCHITECTURE.md)，阶段路线见 [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)。

## 1. 用户已经确定的方向

- 以 M5 为仓库历史基线，未来产品以 OpenAI Agents SDK 为核心，完整重写五份根文档。
- 按 SDK 原生方式从产品需求出发设计，允许从零开始。旧代码能低成本复用才复用，不能复用就放弃；不要求保留旧架构。
- 用户提出把“数据库查询助手”与“慢查询诊断”设计为一个场景；当前草案据此统一为 StarRocks 数据库助手。
- 首版同时支持飞书与 Web 对话，并明确要求最小化。
- 按资深 Python 架构师与 Agent 开发专家标准逐步交付，最终进行实际环境验证。

本版移除了上一稿“必须保留 PostgreSQL、Worker、TaskStore、CAS/fencing 底座”的前提。旧 S0–S6 迁移路线被 P0–P3 产品路线替代，上一稿复用表不再是实施约束。

## 2. 当前书面设计与待评审默认值

设计草案：一个 StarRocks Agent、SDK Runner/function tools/Session、一个 Python 进程、SQLite、同一应用服务供 Web 与飞书调用。

为满足最小化而提出的默认值：本机 Web、飞书获准用户单聊、一个 StarRocks 连接、两端独立会话；查询及普通 EXPLAIN 为必交，已有审计源与 Profile 为环境增强项。新应用建议独立 `src/xiaowei/` 包，不装配旧 Runtime。

上述实现细节是书面设计草案，尚未获负责人整体验收。P1 详细代码任务计划尚未编写，产品代码尚未实施。用户对双入口的确认不写成已接受全部技术细节。

## 3. 当前仓库事实

| 项目 | 已核对事实 |
| --- | --- |
| 仓库 | [shixian66/xiaowei-agent-sdk](https://github.com/shixian66/xiaowei-agent-sdk) |
| 本地目录 | `/Users/kloenguyen/Desktop/agent-SDK` |
| 本轮源码基线 / 拉取时 main | `372c381f44ecfa1fa53961f137d0058033cbd805` |
| 基线提交标题 | `docs(m5): record postgres and compose evidence` |
| 工作分支 | `claude/sdk-core-docs` |
| 本轮变更 | 仅 AGENTS.md、ARCHITECTURE.md、AGENT_HANDOFF.md、README.md、DEVELOPMENT_PLAN.md |
| 新产品实现 | 尚未实现；依赖中没有 `openai-agents`，没有新的双入口 SDK 产品 |
| 当前源码 | 旧 `xiaowei_agent` 包，规则解释器、确定性 Resolver/PlanCompiler/Runner 与 fake 慢查询场景 |
| 其他文件 | 业务代码、测试、依赖锁文件、CI 与 Compose 未修改 |
| 外部操作 | 本轮未运行真实模型、StarRocks 或飞书，未部署、合并或归档 |

M5 是历史起点，历史验收不证明新 SDK 产品可用。保留旧文件仅是当前尚未开始源码替换，不表示新产品将复用全部旧实现。

## 4. 五份文档的作用

- [AGENTS.md](AGENTS.md)：从零设计、SDK 优先、Python 质量、按实际风险验证的工作规则。
- [ARCHITECTURE.md](ARCHITECTURE.md)：查询与诊断统一产品、双入口、原生 SDK、最小存储和执行边界。
- 本文：已确认事项、草案默认值、真实代码状态和证据。
- [README.md](README.md)：新产品介绍与必要环境，明确尚无经验证的新启动命令。
- [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)：P0 设计、P1 双入口查询、P2 诊断、P3 实战试用的交付顺序。

旧 ADR、里程碑和旧启动命令只说明历史系统；当前新方向以这五份文档及用户更新指令为准。历史材料不要求逐条迁移或重新批准才能放弃旧框架。

## 5. 外部依据与版本限制

已核对 OpenAI 官方 SDK、quickstart、orchestration、running agents、results、guardrails/approvals 和 observability 文档；当前方案直接使用 SDK 的 Agent 循环、工具与 Session，采用单 Agent 起步。

已核对 StarRocks 普通 EXPLAIN、EXPLAIN ANALYZE 和获取 Profile 文档：分析计划与实际执行不同，Profile 获取受环境影响。也核对了飞书官方 Python SDK 文档，其 Channel API 存在包迁移；实施时需验证发布包，不能把在线示例当成已安装可用接口。

来源链接集中在 [ARCHITECTURE.md](ARCHITECTURE.md)。尚未安装锁定 SDK、模型、数据库驱动、SQL 解析库或飞书包，也未核对目标 StarRocks 版本与权限；不能宣称依赖兼容性已经通过。

## 6. 验证记录与限制

本版文档改写后实际执行：

- `git diff --check`：exit 0；变更文件仅为上述五份根文档。
- 五份文档的 23 个本地链接、Markdown 代码围栏与占位标记检查：0 个错误。
- `python -m pytest tests/security/test_docs_command_consistency.py tests/contract/test_doc_fact_binding.py -q`：5 passed，exit 0。

测试使用原项目现有虚拟环境 `/Users/kloenguyen/Desktop/agent/.venv/bin/python`，显式设置本仓库 `src` 为 `PYTHONPATH`；未安装新依赖。该结果只证明选中文档检查通过，不证明新工程锁定环境或新产品运行通过。

已自检五份文档的用户范围、SDK 所有权、双入口、会话边界与当前/目标表述。文档测试覆盖命令与部分旧事实绑定，不验证新架构语义；尚无独立审查、SDK 实现测试、全量产品测试、正式浏览器、真实模型、真实 StarRocks 或飞书运行证据。

## 7. 下一步

先审阅当前书面设计，重点是首版用户流程、双入口范围和单 Agent 最小结构。设计确认后，为 P1 写具体实施计划，再按小任务实现。

进入真实联调时需要确认：可用模型与预算、允许外发的数据、StarRocks 版本和获准只读范围、飞书应用及允许的单聊身份。凭据通过本机安全配置引用，不能粘贴到对话或文档。上述环境信息不阻塞设计和独立离线开发，但对应实战退出条件必须保留。
