# 小维 · StarRocks 数据库助手

小维是一款以 **OpenAI Agents SDK** 为核心设计的数据库助手。首版目标是在 **Web 对话和飞书单聊** 中完成“查结构 → 只读查询 → 解释结果 → 分析慢查询”的连续对话。

> 产品方向已确认，P1-A 实施计划已编写、待审阅。仓库源码仍是 M5 历史基线，SDK 产品尚未实现；下文能力描述是首版目标。准确状态与验证记录见 [AGENT_HANDOFF.md](AGENT_HANDOFF.md)。

**OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**

## 首版可以解决什么

- 了解获准 StarRocks 数据库的表、字段和结构。
- 用自然语言生成 SQL，或提交自己的 SQL，在受控范围执行只读查询并解释结果。
- 继续追问“这条 SQL 为什么慢”，查看普通执行计划，得到有依据的优化建议与局限说明。
- 在 Web 或飞书连续对话，查看实际 SQL、有限结果和诊断依据。

查询与诊断共用一个 Agent。首版 StarRocks 使用本地 function tools，同时具备通用 MCP Client Integration，连接未来获准的外部工具服务；首版不自建业务 MCP Server。两条工具路径都在调用前复核权限、结果进入模型前过滤，最终发送前验证 Evidence。模型、会话、Web 与飞书分别接收各自允许的数据。

## 最小产品形态

| 项目 | 首版设计 |
| --- | --- |
| Agent 核心 | OpenAI Agents SDK 的 Agent、Runner、function tools、Session |
| Web | 本机使用的简单对话页，显示文本、SQL、有限结果与执行提示 |
| 飞书 | 获准用户与企业自建机器人的单聊文本消息 |
| 数据源 | 一个服务端配置的 StarRocks 只读连接及明确授权范围 |
| MCP | 官方 SDK 接入能力 + 小维可信配置与治理；配置为空时，本地功能照常运行 |
| 会话 | 两端分别保留上下文，共用业务逻辑；暂不跨渠道同步 |
| 运行与存储 | 单 Python 应用进程；依赖 SDK Session，首版用 SQLiteSession；飞书优先长连接 |

SQLite 是首版实现选择，不是长期架构绑定。生产默认关闭 tracing 与外发；真实数据 trace 需要显式配置允许范围。

已有审计源与 Query Profile 按环境能力接入。缺少它们时，仍可分析 SQL 与执行计划，但必须明确证据不足；不会自动修改目标配置来开启采集。

MCP 负责标准化工具接入，不能替代业务授权。只有参数含义、风险和结果策略已有支持的服务，才能主要通过配置注册；并非填写一个地址就可安全使用任意工具。

首版不执行数据修改、配置变更或优化建议；不建设复杂管理后台、多 Agent 或分布式任务平台。

## 怎样开始

新产品启动命令将在首个可运行切片完成并验证后提供。目前不要把仓库里的旧 CLI、Worker 或 Compose 命令当成 SDK 产品入口。

实施时需要：

1. 本机 Python 开发环境；依赖版本由新工程安装验证并锁定。
2. 可用 OpenAI 模型及 API key 的本机安全配置。
3. 获准的 StarRocks 测试连接、真实只读账号、数据库/视图范围，以及允许向模型与渠道展示的数据。
4. 飞书企业自建机器人、消息权限、事件订阅及获准单聊用户。

凭据仅在本机或部署环境配置，不粘贴到对话、仓库、浏览器或日志。缺少真实环境时可以开发和离线验证，但不能标记对应实战验收完成。

## 设计与开发

- [AGENTS.md](AGENTS.md)：开发规则、SDK 优先原则与工程质量要求。
- [ARCHITECTURE.md](ARCHITECTURE.md)：完整产品设计、工具、双入口与执行边界。
- [AGENT_HANDOFF.md](AGENT_HANDOFF.md)：当前代码与已经验证的事实。
- [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)：逐步交付的顺序和验收目标。
- [P1-A 实施计划](docs/superpowers/plans/2026-09-29-p1a-sdk-governed-core.md)：先验证 SDK、治理与 MCP 核心，再接真实数据库和双入口。

设计直接使用 [OpenAI Agents SDK](https://developers.openai.com/api/docs/guides/agents/sdk) 原生能力。旧实现只在有明确价值时提取少量业务素材，兼容旧框架不是新产品目标。

M5 是 Git 历史起点，不是必须保留的架构。仓库中尚未清理的旧代码、测试、ADR 和里程碑用于历史参考，不能与上述新设计共同作为新产品规范。
