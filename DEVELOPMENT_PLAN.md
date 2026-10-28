# 小维：产品阶段与验收路线

> 本文维护阶段交付、依赖顺序和退出条件，不保存逐次开发日志。当前代码、合入版本、有效证据和下一项工作见 [AGENT_HANDOFF.md](AGENT_HANDOFF.md)；产品范围与权限／数据边界以 [ARCHITECTURE.md](ARCHITECTURE.md) 为准。

**OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**

## 1. 交付目标

以同一个 SDK Agent 交付受治理的数据库与运维调查：StarRocks／TiDB 查询诊断、监控调查、DS 只读排障，以及单独批准的监控写、通知、定时只读任务和有界历史追溯。Web、飞书获准单聊及一个指定群共用应用服务；正式部署为单进程小维与 PostgreSQL 两个容器。数据库本地直连，MCP 用于已核约的外部服务，不建设另一套 Agent Loop 或通用任务平台。

P0–P3 是最初 StarRocks 产品的交付分层；后续增量复用其基础，不要求重做已合入的实现，也不把未完成的真实验收视为已经关闭。某项“本期不做”只表示不开发／验收，不能据此新增代码或提示词禁令。

### 每次开发怎样推进

从 handoff 和当前用户授权选一个可独立验证的单元，沿 [AGENTS.md](AGENTS.md) 执行。先核对锁定 SDK 的公开能力和现有链路，尽早跑通正式入口的成功与关键失败；风险高的边界按精确版本独立审查。缺环境只阻塞相应真实验证，继续独立离线工作；不以脚本模型、协议替身或 CI 替代真实任务能力。固定业务样例从设计沿用到开发、审查和用户验收，不要求非必要的固定工具顺序。

## 2. 阶段与验收

| 阶段 | 用户可见交付 | 依赖与退出重点 |
| --- | --- | --- |
| P0 产品设计 | 文档职责、范围与架构边界一致 | 明确需求和取舍；事实、已批准契约与未实现方向分开 |
| P1 双入口查询与 MCP 基础 | Web／飞书单聊查询及连续追问 | 真 SDK、正式入口、只读工具、MCP 协议、Session／Evidence 隔离；真实模型与双入口任务另留证 |
| P2 诊断闭环 | SQL、计划、布局及 AuditLoader 慢查询分析 | 依赖 P1；诊断不执行原 SQL，依据／推断／缺口分明；两端实战路径 |
| P2.5 多目标与自然语言用途 | 多库复杂 SQL、自动范围、多集群与 Agent 自主选择 | 依赖 P2 离线基线；源隔离、权限／历史复核、资源限制及真实模型澄清 |
| 飞书单群增量 | @ 触发、共享追问、同群排队 | 依赖 P2.5；可信 mention、当前成员、归属、去重和真实平台收发 |
| P3 部署与实战 | 两容器安装维护和公司业务试用 | A/B 为发行与维护；C 为真实目标、模型、渠道、恢复和用户接受，分别判断 |
| P4 监控 | 三源调查及群内逐次批准的选中写动作 | 每源读取与协议 → Action／SDK 暂停恢复 → 该源真实写与回读；不等待无关源验收 |
| 回答、报告与输入增量 | 自然业务正文、v4 按需详情、MD／HTML、群 post 和受限附件输入 | 复用交付／权限链；旧版本兼容、真实模型表达、平台形态与客户端分别验收 |
| 通知与定时 A–D | 即时通知 → 固定提醒 → 独立 Agent 任务 → Node 专项 | 按 A→B→C 依赖；D 暂停，不从 C 可运行推定专项完成 |
| 历史 H1–H3、安全巡检 I1/I3 | 跨会话事实与结论追溯、按本轮证据解释关注事项 | H1→H2→H3；I1→I3→按证据决定 I2，不提前实现长期知识体系 |
| TiDB A/B/C | 业务查询／结构、SQL／慢查询、锁事务／集群诊断 | A→B→C；B4 单独验 T8，公司两版与三集群分配固定样例 |
| DS API 只读 | 环境选择、失败任务与日志诊断、等待／恢复追问 | S1→S2→S3→S4；固定 D1–D4／19 变体，治理和任务能力分别验收 |
| CloudCanal 一期 | 任务／状态／指标、日志与交叉调查 | CC0 现场核约 → 获开发授权后 CC1/CC2 → 获准环境 CC3；探针不是产品接入 |

上述表格是交付结构，完成状态不在这里复制。P1–P3 的未完成实战项可与获准增量的独立开发并行，但必须保留各自退出证据；生产开放范围不由合并或测试数量推导。

## 3. P0：确定设计

核对 SDK 唯一 Loop、单 Agent、共享入口、治理先于 I/O、四种数据边界、Session 与应用表归属及明确的产品非目标。新需求先验证已有能力能否完成；只有具体缺口才新增工具或组件。产品／权限变更须明确批准，技术细节在既定边界内选择最简单的实现。

文档按职责维护：架构保存长期契约，handoff 保存当前事实，README 保存实际用法，本文件保存阶段和退出条件；历史 ADR、旧切片和旧 CI 不覆盖现行契约。

## 4. P1：双入口查询与 MCP 基础

P1-A 建立真 Runner、Model Profile、受治理工具、Evidence 和 SDK Session；P1-B 接 StarRocks Adapter／SQLGuard、Web 与飞书及请求／交付记录。详细依据见 [P1-A](docs/superpowers/plans/2026-09-29-p1a-sdk-governed-core.md) 和 [P1-B](docs/superpowers/plans/2026-09-30-p1b-starrocks-dual-entry.md)，当前入口和存储不恢复旧 Runtime、Worker 或 SQLite 后端；离线反例清单继续沿已链接的 P1-B 计划核对。

退出须同时覆盖：

- 真 SDK 工具往返、结构化回答、连续追问；权限／数据过滤在实际链路生效。
- 真实 PostgreSQL 的提交、回放、过期、容量、失败恢复；去重与渠道隔离。
- 正式 Web／飞书、获准模型和实际只读工具的查结构→查询→追问，记录正确事实、失败与不确定性。
- 每个声称可用的端点／协议／模型组合单独验证；不因其他厂商通过而关闭该组合缺口。

用户曾批准先完成离线实现，把缺少的真实模型和两端任务证据移至 P3-C 补齐；此顺序调整不取消退出条件。自然语言用途、输出结构与批准恢复按当前架构执行，旧 P1“默认诊断”“不恢复 Runner”等约定不再生效。

## 5. P2：同一 Agent 完成诊断

详细任务见 [P2 计划](docs/superpowers/plans/2026-10-02-p2-explain-diagnosis.md)。必交为 StarRocks `EXPLAIN LOGICAL`、表布局、已有 AuditLoader 审计的有界慢查询列表；不执行原 SQL 或 EXPLAIN ANALYZE，不自动开启采集或修改目标配置。Query Profile 暂不实现。后续解析失败自主修正以 [ARCHITECTURE](ARCHITECTURE.md#sql-parse-recovery) 的窄例外为准，不能恢复旧“一切执行后错误均中止”的总括说法。

退出：两端完成“找到慢查询→实测指标→计划／表设计→建议”和“查询→为什么慢→依据→建议”的同会话实战。缺 Profile／审计源、权限变化、旧证据、工具失败及根因未确认时如实说明；优化建议不自动执行。隔离版本的零执行证据不替代公司目标版本的核实。

## 6. P2.5 与飞书单群增量

### P2.5：读取放开、SQL 放宽与多集群（直连）

范围只见 [ARCHITECTURE](ARCHITECTURE.md#p25-scope)，详细任务见 [P2.5 计划](docs/superpowers/plans/2026-10-03-p25-open-read-multi-cluster.md)。依赖 P2 离线基线及隔离真实 StarRocks 的权限／元数据／计划验证；不依赖数据库 MCP。

交付多目标、账号实际 SELECT 范围、结构快照与分页、复杂 SQL 及原文执行、当前权限与旧 Evidence 复核、单 Agent 自然语言选择。原 Task 4 的逐查询 EXPLAIN 准入已取消；普通查询沿权限、SQLGuard 和运行限额，不加风险评分或固定前置取证。

退出：至少 3 个目标的路由与故障隔离、同会话跨目标追问、拒绝时零未授权 I/O、迁移与历史失效成立；资源超限受控失败。环境选择、澄清与意图遵守用真实模型验收，数据库账号实际资源组／限额在目标版本核实；程序只证明治理边界，不证明 Agent 永不误选。

### 飞书单群增量

范围见 [ARCHITECTURE](ARCHITECTURE.md#group-scope)，任务见 [单群计划](docs/superpowers/plans/2026-10-03-feishu-group.md)。依赖 P2.5，交付可信 mention、当前成员校验、群 owner／Session／Evidence、有界 FIFO 和回复原消息。

退出：两人共享追问、同群串行／不同会话并行、重投、排队超限、退群、存储／发送失败及单聊/Web 隔离通过；真实群权限、mentions、成员 API、回复位置和客户端效果在获准飞书验证。后续群 post／图片和 v4 交付按 §8 的专项，不据基础群测试自动判定通过。

## 7. P3：获准环境实战

按 [P3 计划](docs/superpowers/plans/2026-10-05-p3-compose-deployment.md) 分开判断 A 发行、B 维护和 C 公司实战；部署契约只见 [ARCHITECTURE](ARCHITECTURE.md#deployment-package)，命令只见 [README](README.md)／[OPERATIONS](deploy/OPERATIONS.md)。

A/B 交付固定 SHA 的 amd64 离线包、两容器 Compose、配置预检、初始化／迁移、启动／停机、显式清理及配套备份／隔离恢复。升级保留配置、稳定密钥和卷；当前 v4 故障按前向修复保留生产发送／Action 账本，历史恢复实验不授权把生产库倒回副作用发生之前。

C 按各源记录：目标版本、TLS、只读账号、当前权限、资源保护、模型／数据外发范围、真实 Web／飞书任务、失败路径和用户接受。DBA 资源组／大查询限制及权限须按目标类型和版本核实，未满足该目标既定保护门槛时不能开放业务查询。StarRocks 的正常／超限资源对照、TiDB 的获准轻量检查分别遵循各自计划，不能混用。既有公司安装、healthy 和部分使用反馈只覆盖记录的版本／场景。

样例包括正常查询、连续追问、诊断、无数据、危险／越权拒绝、超时、重投、发送失败与重启。另做隔离恢复及请求编号排障，确认不重放中断任务、日志不携敏感内容。每项保留精确 SHA、配置摘要、命令、证据与未触发分支；不索取凭据正文。P1/P2/P2.5 的真实退出条件分别补齐，不用一个总“部署成功”替代。

## 8. P4 监控增量与之后的扩展

**监控。** 范围只见 [ARCHITECTURE](ARCHITECTURE.md#monitoring-mcp)，读取／审批／各写动作及反例见 [监控计划](docs/superpowers/plans/2026-10-09-monitoring-mcp.md)。每源读取、SDK 暂停恢复和该源真实写／回读形成独立闭环；Prometheus 服务端限制、Grafana 组织／插件／写端权限、Alertmanager 认证／HA／业务码分别验收。有限读取失败可继续调查，不扩展为数据库或写操作重试。密码私聊入口尚未实现，监控曲线 D4 仍暂停。

**自然回答与群输入。** 长期契约只见 [用户交付与历史详情](ARCHITECTURE.md#user-delivery-and-history)；实现与固定样例分别见[结果交付](docs/superpowers/plans/2026-10-14-result-delivery-documents.md)、[自由回答结构](docs/superpowers/plans/2026-10-17-free-answer-structure.md)、[原生报告](docs/superpowers/plans/2026-10-19-model-authored-reports.md)、[自主调查与进度](docs/superpowers/plans/2026-10-20-agent-autonomy-progress.md)及[群输入／v4](docs/superpowers/plans/2026-10-22-feishu-input-presentation-repair.md)。正文、报告、进度、图片与平台实收分别验收；旧绑定内容和前向修复不能被排版改动绕过。T1–T5 及原变体沿用，不新增固定工具顺序。

**通知与定时 A–D。** 权限和数据范围只见 [ARCHITECTURE](ARCHITECTURE.md#scheduled-agent-tasks)，技术依据和 U1–U7 见[设计](docs/superpowers/specs/2026-10-21-scheduled-agent-tasks.md)／[计划](docs/superpowers/plans/2026-10-21-scheduled-agent-tasks.md)。A 即时通知→B 固定提醒→C 独立受治理 Agent；自建代理沿既有发送机会与 unknown 边界。模型自主调查、日期解释、30 任务实际并发／成本、代理业务响应和群实收仍须真实验收。D 暂停，恢复需明确授权；生产写逐次审批不继承定时授权。审批与任务状态的公司 C1–C4 样例见[回执修复计划](docs/superpowers/plans/2026-10-20-production-dialogue-answer-repair.md#approval-task-delivery-repair)。

**历史与安全巡检。** H1 读取既有记录→H2 保存有界定时结论→H3 真实业务评测，唯一范围见 [D1–D3](ARCHITECTURE.md#operations-history)，任务见 [R4 计划](docs/superpowers/plans/2026-10-26-operations-history-and-knowledge.md)。安全巡检 I1 成文→I3 真实措辞／背景复用→按结果决定 I2，见[计划 §9](docs/superpowers/plans/2026-10-23-security-inspection-report-presentation.md#security-context-r3)。当前检测聚焦注入／延迟、多语句辅助，固定样例和公司 A0/A3 见[收窄计划](docs/superpowers/plans/2026-10-28-security-inspection-attack-signals.md)／[公司清单](docs/starrocks-security-company-checklist.md)。定时任务仍不读群聊交互历史，B1(a) 不变；Wiki 仅保留调研。

**CloudCanal。** 按[一期计划](docs/superpowers/plans/2026-10-23-cloudcanal-phase1.md)先做用户现场 CC0 核约，确认 6.4.0.2 MCP 链接、只读账号、工具和数据语义；取得开发授权后 CC1 元数据→CC2 日志与交叉调查，再在获准环境做 CC3。独立探针用法见 [README](README.md#cloudcanal-cc0)，不把探针称为已接入产品。固定 C1–C5 和确认节点沿计划维护。

多群管理、跨渠道身份、正式多人 Web、任意导出、新 transport／认证、多 Agent、MySQL 或数据库 MCP 仍按实际需求单独决定。有限报告、通知、历史和定时能力不代表这些通用扩展获准；不再重复规划已经有实现的多集群、指定群或慢查询。旧“DDL 首个写场景／独立写 MCP Server”的设想不再生效，数据库写仍须另行批准。

<a id="tidb-abc"></a>

## 9. TiDB：查询、Agent 诊断与执行前评估增量

范围只见 [ARCHITECTURE](ARCHITECTURE.md#tidb-query-diagnosis)，固定 T1–T8、版本差异与公司清单只见 [TiDB 计划](docs/superpowers/plans/2026-10-24-tidb-query-diagnosis.md)。实现／合入证据集中在 handoff，不再把 B/C 写成本地未合入候选。

目标为 3 个二进制自部署集群（两个 v7.5.3、一个 v6.5.5，含 TiFlash），共享一套 Prometheus／Grafana；标签选择沿既有 Agent 方法，不新增硬映射。用户已批准本地 TiUP 精确两版真实库验证；公司模型与生产只读验收由用户在公司环境执行。

| 切片 | 交付 | 独立退出条件 |
| --- | --- | --- |
| A 查询与结构 | 业务查询、表／复合主键／索引、SQL 保真、历史与双入口 | 三集群连接／TLS／权限／资源冒烟；固定 sql_mode、视图安全探测及 T1/T2/T5，内存保护局限保留 |
| B1–B3 SQL／慢查询 | 受保护计划、准确慢 SQL 排名与调查，包含 TiFlash 计划选择 | 依赖 A；两版子查询／视图零执行边界、T3/T4、缺日志／截断及 DBA 独立基准 |
| B4 T8 基础评估 | 仅补 Skill 和执行前影响／恢复前提分析 | 复用 A/B；主版本 T8.1–T8.7 共 7 变体、9 会话，T2 回归及一个正式入口样例，单独退出 |
| C 锁事务／集群 | 等待方向／时长、热点／Region／Store、TiKV／PD／TiFlash 与监控关联 | 依赖 A/B，T8 另依赖 B4；T6 与 UPDATE 等待变体共用已知等待证据，T7 验正常／缺源／标签；无故障或锁样本保留开放 |

实施依赖为 A→B→C，本地开发、离线／真实库验证和独立审查完成后集中公司验收；A 公司验收不阻塞独立的 B/C 本地工作。每集群做基础冒烟，模型样例按版本分配，不按三集群机械重复。DBA 关键事实与重复次数严格沿计划 §9；代码、真实工具、模型、渠道和用户接受分别记证。生产无自然故障／等待样本时，C 的对应诊断能力可继续未验收，不以正常运行推断可靠。

T8 不新增执行权限或评估模式；普通查询不强制 EXPLAIN，DML／DDL／管理 SQL 只分析不执行。没有实际消费者前不建 PD 客户端、数据库 MCP 或领域调度框架。

## 10. DS：API 只读诊断增量

产品契约见 [ARCHITECTURE](ARCHITECTURE.md#dolphinscheduler-environment)，六项业务工具、本地选择、集中新增限制和唯一 D1–D4／19 变体见 [DS-RO-P1 计划](docs/superpowers/plans/2026-10-28-dolphinscheduler-readonly-api.md#ds-implementation-plan)。实现与审查／CI 状态只见 handoff。

| 切片 | 可独立验证的结果 | 关键退出项 |
| --- | --- | --- |
| S1 | 自然环境选择、轮末提交／暂停恢复、四项元数据工具、分类失败 | 未选／越界零 DS I/O；选择不登记 query；旧暂停记录兼容；部分撤权与历史不可回放分开；源失败只停本轮，日志失败不扩大到全源 |
| S2 | 日志读／搜及长日志中间错误定位 | 有界流式下载、同轮同任务一次下载及失败缓存、16／32 MiB、脱敏和四种投影；超长行／日志头／截断／超时，不把未命中说成不存在 |
| S3 | 子运行定位与带时间的等待依据 | 两版实际字段／HTTP 上界、父事实与关联缺口同时保留；等待时长由带时间日志证明，不能只靠 dependentResult 状态 |
| S4 | 同一 Agent 与正式入口 D1–D4 | 成功与关键失败、环境切换／比较／指代澄清、答案质量及调用／耗时／用量；真实 Agent 与离线治理分开验收 |

按 S1→S2→S3→S4 实施；不新增定义目录、下载信号量、HTTP 轮内计数器或第二 Loop。DS 专属选择边界不改数据库行为，SDK 表不改；定时自动集合本期保持不含 DS。C1 四项源身份绑定、C2 删除多余选择字段的决定已进入架构，不再作为未决产品问题。显式 diagnose／带图轮暴露 DS 的源码冲突见 [handoff](AGENT_HANDOFF.md#ds-diagnose-contract-gap)，不因 S1–S4 离线通过而关闭。

**后续用途修复切片（决定已确认，代码待修）。** 按上述 DS 架构契约，将 `ds | selection` 移出 diagnose，复用现有用途集合与 I/O 前治理，不新增路由或状态机制。本轮只更新文档；后续实施时，验收 Web“仅诊断”、飞书 `/诊断` 及复用 diagnose 的图片／TXT/MD 入口：不暴露 DS 工具，强行调用也须在 I/O 前拒绝；普通 query 的 DS D1–D4／19 变体及既有数据库诊断、获准历史读取不退化。离线检查覆盖真实应用装配、入口与治理路径；真实模型、公司 API 和平台实收仍按下述门槛单独验证。

阶段退出仍需获准真实模型、两版公司 API／业务码、每源冒烟、服务端长日志负载与脱敏延迟、正式 Web／飞书和用户接受。用户选择本地仅做离线开发，不能用合成 HTTP、脚本模型或 CI 关闭这些实战项。合并、发行、公司部署和用户验收分别记录。
