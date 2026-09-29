# 小维：当前交接

> 更新：2026-09-29，Asia/Shanghai。这里只记录当前事实、证据与下一项工作；设计见 [ARCHITECTURE.md](ARCHITECTURE.md)，路线见 [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)，协作规则见 [AGENTS.md](AGENTS.md)。

**OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**

## 1. 当前快照

| 项目 | 已核对事实 |
| --- | --- |
| 仓库 | [shixian66/xiaowei-agent-sdk](https://github.com/shixian66/xiaowei-agent-sdk) |
| 本地目录 / 分支 | `/Users/kloenguyen/Desktop/agent-SDK` / `claude/sdk-core-docs` |
| M5 历史起点 | `372c381f44ecfa1fa53961f137d0058033cbd805`；不是远端当前 main 的核验结论 |
| 本次文档审查起点 | `28ce01f01976fc458b8df278c3e77f4a80e4879a`；开始时工作树干净 |
| 当前阶段 | 新方向及 P1-A 书面计划已有；P1-A 源码任务尚未开始 |
| 当前源码与依赖 | 仍为旧 `src/xiaowei_agent/`；`pyproject.toml` 和 `uv.lock` 尚无 `openai-agents`，打包与 CLI 仍指向旧包 |
| 新产品入口 | 尚无经验证的 SDK 产品安装/启动命令；旧 CLI/Compose 不算新入口 |
| 本次工作范围 | 用户确认七项最小方案后，更新五份根文档及现有 P1-A 计划；没有改业务代码、依赖、测试、CI 或 Compose |
| 外部操作 | 未调用真实模型、StarRocks 或飞书；未 push、合并、部署或归档 |

表中的 SHA 是本次审查起点，不自称包含尚未提交的文档修改。接手先用 `git rev-parse HEAD` 和 `git status --short` 取得实际版本；本文件的修改历史由 Git 保存。

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

唯一详细计划：[P1-A：SDK 与治理执行核心](docs/superpowers/plans/2026-09-29-p1a-sdk-governed-core.md)。计划已编写，任务复选框均未完成；当前不标记为已实施或已验收。任务顺序为 **Task 1 → Task 1B → Task 2–5**，共六项。默认在当前任务中串行实施，执行方式不需要单独再选一轮。

**进入实施后的第一项是 Task 1：锁定 SDK 并验证公开扩展点。**

- 交付可独立安装的新包与真 SDK 契约验证：Runner 工具闭环、SQLAlchemySession + 隔离的真实 PostgreSQL、MCP 调用前/返回后拦截，以及默认无 trace 外发。只增加测试数据库 Compose，正式双容器产品另行交付。
- 不接真实 StarRocks、不构建 Web/飞书、不预建 Action；先证明 SDK 原生接口能承载已有边界。
- 先核对 Python/SDK/SQLAlchemy/asyncpg 兼容性及开发依赖安装。当前 `dev` 是 optional extra，计划已统一显式 `--extra dev`；Task 1 仍须实装核对，保证新环境具备 pytest、Ruff、mypy，不能借用旧环境掩盖缺失。真实 PostgreSQL 未就绪不能将存储测试 skip 成通过。
- 通过后再做 Task 1B 模型 API 接入；一家具备获准配置即可先做合成工具试跑，其他厂商分别验证。完整治理、Evidence 与 Session 路径仍在 Task 5 验收。

P1-A 是内部核心。P1-B 才接真实查询与双入口并切换正式入口，P2 增加诊断，P3 做实际用户验收。环境缺失不阻塞独立离线任务，但不能跳过对应实战退出条件。

## 4. 实测环境与兼容性缺口

| 对象 | 当前状态 | 实测前所需信息 |
| --- | --- | --- |
| OpenAI 模型 API | 未实现、未实测 | 获准端点/协议/模型 ID、凭据安全引用、数据范围与预算 |
| Gemini 模型 API | 未实现、未实测 | 同上；单独验证工具续轮、结构化结果与协议字段 |
| DeepSeek 模型 API | 未实现、未实测 | 同上；单独验证 JSON mode、工具与推理参数组合 |
| PostgreSQL / SDK Session | 新后端尚未实现、未实测 | Task 1 建立隔离测试库、锁版验证公开 Session 接口；后续补表版本、保留期与恢复验证 |
| StarRocks | 新产品未联调 | 目标版本、测试连接、只读账号、获准库表/视图、数据投影范围与简短业务口径 |
| 飞书 | 新产品未联调 | 应用与事件配置、获准租户/单聊用户、可信身份来源 |
| 本机 Web | 新产品未实现 | P1-B 落实正式启动、身份/会话边界及浏览器实测 |
| Docker Compose | 新产品双容器尚未交付 | 应用镜像、PG 持久卷、loopback/SSH 访问、启动检查与备份恢复实战 |

凭据只在本机或获准部署环境安全配置，不粘贴到对话、仓库或日志。未提供的环境信息不是用户已授权向任意服务发数据。

## 5. 验证记录与限制

本次六份文档修订后执行：

- `git diff --check`：exit 0。
- 六份 Markdown 的 26 个本地链接、代码围栏、最高原则、旧后端/回答类型残留、计划开发依赖参数与任务完成标记检查：0 个错误。
- `python -m pytest tests/security/test_docs_command_consistency.py -q -p no:cacheprovider`：2 passed，exit 0。使用旧项目 `/Users/kloenguyen/Desktop/agent/.venv/bin/python`，设置 `PYTHONDONTWRITEBYTECODE=1` 与本仓库 `src` 的 `PYTHONPATH`，仅验证现有文档约定，不证明新产品独立环境通过。

人工自查覆盖七项确认内容、SDK 接口与存储所有权、阶段任务对应、回答类型与失败路径。文档检查不验证新架构可实现性，不运行与本次改动无关的旧业务全量测试。

已核对的源码事实来自本仓库 `pyproject.toml`、`uv.lock`、打包入口及旧 CI。P1-A 计划的命令是待实现环境的约定，不是当前已经通过的检查。

前期已查阅官方 SDK、模型、MCP/Session、StarRocks EXPLAIN/Query Profile 与飞书接入资料；链接保留在架构文档。本次核对官方 [SQLAlchemySession](https://openai.github.io/openai-agents-python/sessions/sqlalchemy_session/) 的 PostgreSQL、异步驱动和引擎接入说明；文档依据不替代锁版后的扩展点及真实 PostgreSQL 实测。

尚无新产品独立审查、SDK 实现测试、真实模型、真实 StarRocks、正式浏览器、飞书运行或用户验收证据。本次自查不冒充独立审查；未来生产 Action 仍只有设计约束。
