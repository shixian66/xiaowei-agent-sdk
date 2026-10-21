# 小维：当前交接

> 更新：2026-10-22，Asia/Shanghai。本文维护当前代码状态、有效证据、缺口与下一项工作。产品契约见 [ARCHITECTURE.md](ARCHITECTURE.md)，阶段与退出条件见 [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)，使用与运维见 [README.md](README.md) 和 [OPERATIONS.md](deploy/OPERATIONS.md)。

**OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**

## 1. 当前快照

| 项目 | 已核对事实 |
| --- | --- |
| 仓库 | [shixian66/xiaowei-agent-sdk](https://github.com/shixian66/xiaowei-agent-sdk) |
| 产品代码基线 | B 基于已合并 A 的 `main@9284688bb8907c9968e84d5e49c23e551c53ac58`（PR #123）；隔离工作树 `scheduled-agent-design/agent-SDK`，分支 `codex/one-time-reminders`；原目录已有修改保留 |
| 源码与检查 | 当前源码仅保留 `src/xiaowei`；旧工程及专属测试、Compose、迁移入口、`compose-smoke` CI 和 Alembic 依赖已移除。保留产品测试、公共检查与旧回答兼容路径；旧工程历史可从 Git 查回 |
| 当前存储与回答 | B 候选应用 schema v14（主线 v13），新增一次性任务、唯一发送关联和派生保留标记；SDK 表不变。可信 `answer_version=3`、2/Legacy 与分项交付兼容契约不变 |
| 锁定依赖 | SDK `0.22.3`、Lark Channel `1.4.0`、MCP `2.2.0`、sqlglot `30.17.0`、`nh3 0.3.7`、`tinycss2 1.5.1`、Pillow `13.0.0`、certifi `2026.10.19`（后两项为严格审计发现的既有漏洞定向升级） |
| 正式入口 | `xiaowei serve`、`config check`、`model check`、`storage init/upgrade/cleanup`、`requests resend`；命令与环境准备以 README 为准 |
| 最近有记录的公司部署 | 既有交接记录称 PR #110 / `4e0ab86` 已部署，并记录了部署后的回答拒收反馈。该历史记录不证明当前镜像、配置或健康状态，也不证明 #111–#114 已部署 |
| 当前验收结论 | A 已合并且 CI 9/9；B 本次修复通过独立复审，当前离线检查见§5；真实 U5 未验收，C/D 未实现 |
| 当前任务授权 | A 推送/PR/合并已完成；本轮仅授权 B 审查修复、离线测试和本地提交；不推送、建PR、合并或部署。真实模型、公司源、真实 Webhook和部署仍未授权；生产写逐次审批。隔离环境按锁文件安装，测试使用仓库标识的隔离 PostgreSQL |

原工作目录的 ARCHITECTURE 差异与未跟踪计划/公司配置保留，未复制到本工作树；本次未读取公司配置凭据。旧回答修复计划及验证记录保留为历史证据，不能作为本次定时任务的实施授权。当前任务只按下面的新设计/计划接续。

本文件的状态以表中的核对基线为限。接手者先核对实际 HEAD、工作区及用户新指令；合入、独立审查、离线测试、真实运行、部署与用户接受分别记录，不互相替代。

## 2. 已确定的产品边界

本文只索引仍影响接续工作的决定，不另维护一套架构契约：

- 一个业务 Agent、SDK 唯一 Loop、SDK Session + PostgreSQL、受治理执行与四种数据边界，见 [ARCHITECTURE §3–§9](ARCHITECTURE.md)。StarRocks 继续本地 Adapter 直连，数据库 MCP 暂缓。
- 普通消息由单 Agent 判断是否查询；显式诊断入口才确定性隐藏并拒绝实际查询。环境选择目前依靠会话历史与随包 Skill，执行层没有“用户选定环境”硬校验；接受风险与第二步触发条件见 [R7](ARCHITECTURE.md#r7-targets)。
- SQLGuard 检查业务 SQL 原文，物理表必须带库名；唯一候选可由 Agent 补全后重试，不能把旧的自动补库或 AST 重打印恢复为当前行为。查询资源边界和已接受的视图、审计数据范围见 [§6](ARCHITECTURE.md#6-只读查询保护)。
- S3 允许模型组织文字与事实块；需交付结果漏引时由代码补全，不再用 `uncited_query` 拒收。权限、Evidence、未完成执行和存储边界仍生效；模型文字真实性通过任务评测检查，不能把结构通过当作结论正确。见 [结果交付](ARCHITECTURE.md#result-delivery)与 §8–§9。
- 进度的单聊/群聊差异延迟、仅真实变化更新、正常收尾及异常陈旧已由用户确认，详见[进度契约](ARCHITECTURE.md#progress-observation)；P1 已有离线实现，当前证据见计划 §7。普通消息的区域身份及正文/明细分工见[普通消息契约](ARCHITECTURE.md#ordinary-feishu-expression)，不能由 HTML/MD 报告完成推定达标。
- 监控写仅限已批准动作，使用 SDK 原生审批暂停/恢复及既有 Action 治理；生产开放仍须满足对应源和群审批门槛。Grafana 数据源密码私聊命令尚未实现，目前由管理员在 Grafana UI 填写。见 [监控边界](ARCHITECTURE.md#monitoring-mcp)。
- 内网 Web 无登录、访问者共用操作者权限；目标账号、网络、Grafana 插件/写端及数据源更新竞争窗口等已接受取舍按架构执行。
- 通知及定时任务的方向与待审权限增量见 [ARCHITECTURE](ARCHITECTURE.md#scheduled-agent-tasks)：多个目的群不扩大操作入口，任务独立于原聊天，创建及每次执行复核权限，生产写不能继承周期批准。A 已合入主线，B 为当前离线候选，C/D 未实现。

## 3. 当前计划与下一项工作

### 通知与定时任务：B 一次性提醒候选

A 已通过独立复审并合入 [PR #123](https://github.com/shixian66/xiaowei-agent-sdk/pull/123)，B 仅在隔离分支开发。产品与默认值唯一来源为 [ARCHITECTURE 通知与定时任务](ARCHITECTURE.md#scheduled-agent-tasks)，批准设计/实施计划仍为 v1.4。当前不提前实现 C/D，不调用真实模型、Webhook、飞书、公司源，不读取公司配置或凭据。

B 复用真 Runner、SDK 原生暂停/批准恢复与 ActionBinding，确认一次后持久化不可变 job。固定提醒到期直接走原有 GovernedTools notify、相同 HTTP 适配和发送记录，不调用模型，不重建 Agent Loop。状态/暂停/取消由创建者或个人 grant 管理员且当前原群成员操作，已知 ID 查询；不提供列表/编辑/恢复工具。起草支持模型修正日期格式，未知目标/接收权限不足明确拒绝；低风险管理拒绝只返回安全状态，不中断整轮、不伪称未发送。

新增 schema14、默认关闭的同进程到期检查及唯一 job 发送关联；复用全局槽、实例锁、readiness 及 A 的关键回执交付；共享未知门槛只拦即时通知，不拦分别批准的固定提醒。只对未开始且窗口内的提醒再核验；已占用后超时或重启恢复为 unknown，不重 POST。定义依赖由实时 replayed 回调冻结，按原身份/Session 归属和当前源/接收权限复核；数据期限同步约束任务正文、Action/恢复材料、派生 Evidence、模型历史与请求缓存，显式清理不自动新建上下文。

B 已包含两项兼容修复：通知准入必须检查 InstanceLock.admits 返回值及 readiness；发行元数据不能硬编码 schema12，改从源码唯一 APP_SCHEMA_VERSION 读取，保持打包脚本仅用标准库。

### 已合入的监控可靠性修订（PR #121）

产品只改 `app.py` 的两段监控指令和 `runtime.py` 的即时查询说明；未修改执行逻辑、治理策略、Evidence/Session 或交付。评测复用现有 `scripts.dialogue_real_model`，新增 `monitoring-reliability` 集；领域判据只展示给评审者，不进入 Agent 指令。

外部审查在 `23c7185f38ade6c89d5a2fecfc93f60916db952d` 发现 `test_monitoring_read_recovery.py` 仍断言已删除的旧句，作者复现该测试的两个参数用例均失败。初版180项通过的记录漏了这个直接检查监控指令的文件，初次独立审查的“无阻断”结论也被该反例推翻，不能作为当前候选通过依据。本次只删除已有新契约测试覆盖的旧句断言，保留故障源隔离、成功证据保存和历史读取等行为断言，不把旧句加回产品说明。

同时补齐history的单点up反例、scope扩围后的fat1资源未核实判据，并在评测文档说明复用字段的本套件含义。现为9场景/12轮；相对 `23c7185`，`src` 与采集器执行逻辑未改，产品仍仅三处文字替换。

补充回归：`test_monitoring_read_recovery`、`test_monitoring_reliability`、`test_dialogue_real_model`、`test_prometheus_p2`、`test_database_skills`、`test_dialogue_contract`、`test_app`、文档命令检查合计 **220 passed、27 skipped、3 deselected**；27项均因官方 MCP 二进制未配置跳过，3项browser未选。`ruff check --no-cache .`、`mypy src`（35文件）、`git diff --check` 通过。没有新的全仓测试、CI或真实模型证据。

独立复审重新检查 `23c7185..759c644790f280fc1cc7771502d6ed3d2195d699`，另复验30项通过（含原失败的两个参数用例），确认旧断言删除没有削弱必要行为检查，P1关闭、当前修订无阻断。文字引用搜索和测试字符串AST对照未发现其他直接依赖被删文字的遗漏。复审后只更新本交接记录，产品、采集器和测试未再变更。

该修订已由 PR #121 合入 `main@143c4c8`；具体样例、依据及验证边界见[监控可靠性评测说明](docs/monitoring-reliability-evaluation.md)。真实模型对比仍需另获 Profile、端点/模型与费用授权。本套固定导出解释题不能替代自主查询、补证及真实源/飞书验收；求值窗口展示标签的问题未在该修订中解决。本次保留这组已合入的指令、评测和验证记录，不据此关闭真实验收。

### 已合入的相关工作

下表列主线合入版本与关键接续证据；测试与运行边界见后两节，详细实施仍查对应计划。

| 工作 | 主线提交 / PR | 接续说明 |
| --- | --- | --- |
| P2 / P2.5 / 飞书单群基础阶段 | P2 `85d657c` / #21、#24–#30（方向文档 #22、#23）；P2.5 `a938a0f` / #32–#41；单群 F1–F4 `f32291b` / #44–#47 | 阶段代码与离线退出记录的检索入口；P2.5 原 Task 4 已取消，真实退出条件仍按 P3 分项验收 |
| 生产对话回答修复 P2/P1 | `821ee8d` / [#119](https://github.com/shixian66/xiaowei-agent-sdk/pull/119) | 精确查询指引、brief 与正文/详情、可信短提醒及动作边界已合入；CI 与未关闭的 P4 条件见 §5 |
| 数据库环境选择第一步 | `1378b75` / #108 | 两份数据库 Skill、环境规则与评测框架已合入；真实遵守率未验证，不再列为待实现或待推送 |
| 生产轮次失败修复 | `4e0ab86` / #110 | 有界模型请求重试、监控只读失败反馈、选择器修正与最终回答改写已接入；原三选一回答约束已由 S3 替代 |
| 引用契约修复 | `c00ede6` / #111 | 一次无工具改写及安全日志已实现；其旧的强制引用/分析关联规则由 S3 替代，不重复实现 |
| 自由回答 S1 | `9231f45` / #112 | 小行为修正已合入；也是现有计划指定的真实评测基线 |
| 自由回答 S2 | `897ee83` / #113 | schema 与旧 RunState 恢复离线实验已合入；不能证明真实 Vertex 接受新 schema |
| 自由回答 S3 | `6c585d0` / [#114](https://github.com/shixian66/xiaowei-agent-sdk/pull/114) | 新回答、结果补全、来源分区、兼容与正式入口审批恢复已合入。Codex 对 `eff89960` 独立复审通过；[CI 39131473170](https://github.com/shixian66/xiaowei-agent-sdk/actions/runs/39131473170) 10 项全部通过、无重跑；以该 head 做 squash 合并，`6c585d0` 与候选文件树相同 |
| 飞书结果交付 | #102–#105；平台门槛修复 `643858f` / #107 | 分项回执、完整文件拆分、自然正文与容量门槛已合入；真实平台和客户端验收仍开放 |
| 模型自主报告 | `f7eabe6` / #116 | MD/静态 HTML、短 Skill、安全预览与回答版本 3 已实现；R3 真实模型/平台验收开放，不重复列入本次待开发 |
| Vertex 诊断 | `76850bf` / #117 | 安全 429 分类、HTTP 尝试和轮次 usage 日志已合入；不是用户进度事件流，不改变模型/工具执行策略 |
| 监控写与 SDK 复用 | D7 `7b66738` / #83，SDK-A/B/C #89/#93/#94，C8 `408fafb` / #95 | Dashboard、数据源非秘密字段、Annotation 与原生审批已有实现和离线/隔离协议证据；公司生产写开放不能由此推定 |
| 监控曲线 D4 | `eda4a0b` / #106 | 协议前置核验完成，用户已决定暂不实现曲线；不作为当前待实现事项 |

P1-B、P2、P2.5 与飞书单群的基础切片已有离线交付记录；P3-A/B 部署维护代码已合入。P1 Task 9、P2 诊断、P2.5 环境行为及单群的真实退出条件仍按 P3 分项验收，不把这些阶段统称为全部完成。

### 当前回答结构与主要证据

已合入的回答结构见[自由回答结构计划](docs/superpowers/plans/2026-10-17-free-answer-structure.md)。其中 S2 的“留 S3”等阶段文字是当时记录，当前合入状态以上表为准。

已合入的[模型自主 HTML/MD 报告](docs/superpowers/plans/2026-10-19-model-authored-reports.md)复用 blocks 和现有治理/交付；MD 原生排版、独立 HTML 片段与短 Skill，事实就近由代码填入，主消息提取文字。版本 3 贯穿保存/读取，解决旧纯文本在升级时未绑定字节的兼容问题；旧 HTML 基线字节回归保持一致。禁止脚本/外部资源，解析 CSS 值控制布局，文件/试排/预览只清理一次。A1 仍保留通用模型失败回执，未扩大数据/权限或 SQL 取数限额。

普通消息保留自由块顺序；本次 P1 复用 full/brief，本轮 brief 收起且完整交付，历史 brief 保持计数摘要及不自动导出原值的边界。remaining 与普通 metadata 默认收起；完整详情超卡片容量时沿既有分项附件交付。新 HTML 用静态详情，新 MD 分正文与证据明细文件，用绑定正文索引和文件内标题标明用途；旧绑定字节、键、uuid 不重排。未新增模型 schema、回答/数据库版本、持久化展示标志或展示服务；Web 保留 textContent。动作与审批材料沿完整专用契约。实现与离线验证不代表真实模型或飞书客户端达标。

Vertex 诊断已随 #117 合入。其候选历史记录为合成响应与应用测试 **344 passed**，ruff、mypy（34 文件）通过；真实 429 样本、公司环境和部署仍未验证，本轮未重跑该套件。不解析日志充当进度回调；新增观察只使用 SDK 与实际应用事件。

S3 的源码事实：`AgentAnswer` 为有序 `TextBlock | FactBlock`，另含 `report_format` 与 `awaiting_user`；模型文字累计上限为 16,000 UTF-8 字节、最多 64 块。归一化、输出预检、Session 提交与交付复用同一规则；本轮新事实的 brief 收起但完整交付，历史仍按可信原执行轮次生成摘要，重复引用取 full，不新增模型调用。符合补全类别的本轮结果进入“其余结果”，历史结果与未引用的诊断/发现原值不补全。动作区域置顶且不进附件；暂停材料显式引用本次请求执行的动作及本轮动作状态，不自动补入其他未请求执行的提案，暂停前已取得的需交付只读结果仍由交付补全。

`repair_final_output` 已通过 Runner 的 `invalid_final_output` 接入一次无工具改写；失败时不重放工具，被拒的原回答不进入后续历史。旧回答投影不套新输出预算，已绑定报告与分项重发复用原字节。方案 1 已有正式群入口的离线恢复证据：旧历史/暂停材料的批准、拒绝、重复/并发批准、发起人失效；无需据旧阶段描述重新实现恢复方案。

### 下一项工作与开放条件

B 的当前离线单元已交付；真实 U5 的日期解释、工具选择、答案质量、调用/耗时/用量及测试机器人接受/群内表现仍须具体授权后验证。保持默认关闭，不自动部署或把 C/D 当作已经实现。下一切片是 C 的独立受治理 Agent 任务；以下为并存的旧业务实战缺口，不作为 B 已验收证据。

本次 P2 已在 `1c06a89d633753f70fb6e5829c0cc4fc5992cbfd` 独立复审通过，两个阻断（合法反引号 SQL 的夹具路由、普通 naive DATETIME 错标会话偏移）已补回归并修复。普通查询保留无偏移墙上时间，aware 时间及已知来源模板不变。旧历史/绑定附件可能保留原先错误偏移，本次不追溯修正、不批量失效旧 Evidence。

P1a/P1b/P1d 首候选 `aa95773` 曾通过独立审查；后续反伪造反例发现折叠入口统一置后，未保留事实块位置，原通过结论随此反例收窄。修订将既有详情构造就地调用，旧 v0 仍置后；作者 36 种及独立审查者 22 种 v0 差分输出均一致。原字节重发、权限、审批与容量路径由扩展回归覆盖。已逐项核对批准计划 A2 的 Evidence 归一化与投影、`_raw_redundant`、`_data`、`fold_details`、卡片摘要/技术详情/容量捷径、自动报告参数、报告事实章节及 Web metadata；共用 `historical_brief` 只读属性据已有 summary 判定，不参与序列化。新 `brief` 无摘要仍有原值；显式历史导出正文保留摘要、原值放详情；自动历史摘要缺原文时有明确提示。

B1 的短口径主视图修复已成立；原 B2 因复现形状不符合真实 validate_answer 而撤回。N1 撤回多余的动作报告收集与特殊渲染分支，保持 ARCHITECTURE 的“动作区域不进入数据附件”，不改变契约或已绑定文件原字节。P2/P1 已随 #119 合入，原精确 SHA、默认全量与复审结果见 §5。当前 P4 候选补齐六轮对话与同类场景、实际正文/文件人工核对、绑定回执指标、双源局部失败和正式 Web/HTML 浏览器回归；离线链路成立不等于模型能力验收。具体采集/测试命令见 README；真实模型与客户端联合验收仍需具体环境及费用授权。不部署。A1 目标 StarRocks 的 `CURDATE()/NOW()`、时区与 DATETIME 语义仍开放：审计写入时区非 Asia/Shanghai 时，普通 naive DATETIME 保留审计墙上值且可信表格无时区标注，须实测正文时区解释或获准 CONVERT_TZ 查询。P4 必须验证模型遇到直查可纠正拒绝后不偷换候选工具；该行为目前依赖指引，离线执行失败检查不证明模型遵守。真实模型任务能力、飞书桌面/手机多附件与详情展示、用户验收均继续开放。P3、P1c 不在本次范围。既有自主调查与进度工作仍保留以下验收条件，不据本次查询和布局测试关闭。

1. P1 的审查修复只调整正常在途发送的顺序收尾、暂停结果投影与测试同步；失败/超时/取消仍停用，正式回复不等待进度。P1、P2、M1a、M1b 的离线证据见[实施计划 v1.7](docs/superpowers/plans/2026-10-20-agent-autonomy-progress.md) §7。下一项是由用户在公司环境沿同一 T1–T5 执行 P3，核对真实模型任务质量、平台权限、修复后的收尾与客户端体验；已批准产品取舍不重开。
2. M1b 的七项验收已有离线证据，折叠、依据去重、附件完整与失败/未知恢复已覆盖；真实桌面/手机上的明细可达、代码复制及表现仍须 P3 验收。该历史切片不改变 schema；本轮 brief 已按用户确认改为收起且完整交付，历史 brief 摘要及导出边界保持，不为布局增加查询。
3. 报告 R3、S4 的真实 provider/schema、模型质量及界面验收继续开放，执行时复用 T1–T5 和已有评测，不另建平台。原 S0 指定 `9231f45` 的历史基准安排不由本计划自动改变；本次进度对照基线为 `76850bf`，两种对照须明确差异与用途。
4. 真实 Profile、数据/接收范围、预算与安全引用须具体获准。已有模型、公司部署或监控写的历史授权不扩展到本轮；缺环境仅保留对应验证缺口。

报告 MD 拒收、N1 提示和已明确的 HTML 可见性修复已合入，不重开已关闭问题；历史审查覆盖、未覆盖的任意配色对比度和公司内网 R3 测试安排见报告计划 §9。该历史安排不代表本轮取得调用或部署授权。

暂缓：监控曲线、SQL 大结果限额调整、任意全局 HTML/CSS/交互页面、数据库 MCP 及其他未批准写动作。数据源密码私聊命令有设计但未实施。

## 4. 实测环境与兼容性缺口

下表区分代码/离线证据、隔离真实软件和公司运行。既有测试与用户反馈仅覆盖注明版本与范围，不自动覆盖当前版本验收。

| 对象 | 已有证据及范围 | 未关闭项 |
| --- | --- | --- |
| OpenAI Responses | SDK 路径与 HTTP 替身验证 | 获准端点/模型的真实工具闭环、结构化输出与 Session 追问 |
| Gemini 兼容端点 | 2026-09-30 历史真实 API 部分验证：工具选择、签名回传、类型化回答；当时受 503/429 影响未闭环 | 当前具体模型的完整续轮、交付与签名回放；不把历史额度或模型可用性判断当作今天的事实 |
| DeepSeek | 协议替身验证；历史第三方中转 V4 Flash 带输出格式时未调用工具，被判为该组合不兼容 | 官方端点未实测，其他端点/模型不能继承第三方结论 |
| OpenAI 兼容网关 Gate 0 | `6389d78` 上 `glm-4.7-flash` Profile 的一次获准真实运行，使用合成工具/数据；当时临时凭据文件已删除，密钥作废由用户负责 | 只证明该历史组合，未证明稳定质量、真实 StarRocks 或飞书闭环 |
| Vertex | 公开 Model 适配、多调用/签名回传及新 schema 转发已有离线协议证据；既有公司结果与 429/回答拒收日志证明曾实际调用 | I-V B/C 未完成；S3 `anyOf` 接受、模型遵守率、改写与退避效果、旧历史影响、12 图请求仍待对应真实验收 |
| PostgreSQL / SDK Session | 历史隔离 PostgreSQL 16.15 覆盖初始化、迁移、密钥绑定、备份/恢复及拒绝路径；后续 v10 暂停恢复、v11 报告、v12 分项交付与 S3 旧格式兼容亦有测试记录 | 公司环境备份保留、容量、故障主机恢复与操作者演练；新 PG 镜像版本另验。schema v12 的存在不证明任意升级路径都已验收 |
| StarRocks | 可丢弃 4.1.4 容器与合成数据验证只读工具、权限、EXPLAIN LOGICAL、复杂 SQL、视图/审计及原文执行；S1 真协议结果见 §5。公司对话中有实际查询与环境误选反馈 | 公司目标版本、TLS、账号权限、资源组/大查询限额及 P3 固定任务集仍需成套证据；本次 SQLite 替身不支持的合法语法亦须实际复核（见计划 §7），历史反馈不能替代这些退出条件 |
| Prometheus MCP | 官方 v0.18.0 二进制连接本机合成 API，真 SDK 协议、治理及有限修正已有记录 | 公司服务端超时/样本/并发限额、真实指标调查质量与异常形态 |
| Grafana MCP / Grafana | 官方 MCP v2.0.2；读取有本机协议证据，D7/C8 在可丢弃 Grafana 13.2.3 与隔离 PG 上有实际写入/回读及拒绝证据 | 公司组织/账号/插件/网络、写端认证和生产开放门槛；CI 未配置官方二进制/隔离 Grafana 的用例可能跳过，不能以 CI 绿代替 |
| Alertmanager | API v2 替身及早期官方 v0.34.1 隔离协议记录；后续读取修正另有正式入口离线证据，已有公司错误反馈 | 公司版本、认证、HA、容量、写后回读及群审批验收；没有证据覆盖的版本组合不认定通过 |
| 飞书 / Lark | 官方 SDK、长连接/消息协议替身、群入口、卡片/post、附件与分项恢复有离线证据；已有公司使用反馈 | 正式应用 create/reply/apatch 权限、回执/限频及原群回复位置、修复后的正常收尾、真实并发对正式发送的影响、桌面/手机折叠与代码复制、附件预览/文件一致性仍须 P3 验收；长连接重投、30,000 字节外的平台限制、uuid 去重、多文件时延和图片事件/模型识图沿原缺口保留 |
| Web | 正式入口及真实 Chrome 的离线用例，模型与数据库按具体用例使用替身/隔离服务；S3 浏览器记录见 §5 | 当前版本真实模型下的业务验收及大量事实展示专项；端口占用未运行的用例不计通过 |
| Docker / 公司部署 | P3-A/B 在本机 Linux/arm64 两容器验证升级、回退与隔离恢复；发行支持手动生成 amd64 离线镜像包。既有交接记录有公司安装/healthy 和 `4e0ab86` 部署反馈 | 后续版本部署、当前运行状态、真实路径、恢复与用户接受逐项记证，healthy 不等于业务验收 |

凭据仅在获准环境通过安全引用配置，不索要或粘贴明文。未提供环境信息不等于授权连接任意端点。上述实际软件/协议测试不等于公司服务验收，已有运行反馈也不能再总括为“从未运行”。

## 5. 验证记录与限制

### 2026-10-22 B 独立审查修复

主线基线 `9284688bb8907c9968e84d5e49c23e551c53ac58`；本次审查输入 `52f7b0e6d968c249241d83e59957671824ec8970`，产品修复提交 `54a27161cc0ec8c8a0ae9f99687ac603e9860bf0`（以下正式检查与独立复审版本）；后续提交只更新交接/计划状态。按 B1/C1/N1–N3 修复，用户本轮批准的两项契约已同步 [ARCHITECTURE](ARCHITECTURE.md#scheduled-agent-tasks)：分别批准的提醒不受其他 unknown 门槛拦截，但自身 unknown 仍阻断即时通知；批准达到或超过提醒时间拒绝激活。生产写、当前权限、接收范围、数据 TTL 与 Evidence 验证不放宽。

- B1：`scheduled_tasks.propose` 将正文期限改为依赖的最早数据期限；发送截止另由 run_at＋lateness 计算，tick/claim 各在自己的状态变化边界核对。七天历史＋近时提醒过发送窗口仍可回放，数据到期仍清理派生副本。没有新增期限列。 回归 `test_reminder_send_deadline_preserves_valid_history_and_data_ttl`、`test_send_deadline_is_rechecked_at_atomic_claim`；两种期限隔离变异均被检出。
- C1：固定提醒 claim 不再读取即时风险门槛；同任务唯一 attempt 和发送前持久化不变。正式入口分别创建/批准两份任务，先 unknown 不作废后者，即时发送仍先提示。 回归 `test_approved_reminders_ignore_other_unknown_but_immediate_send_still_warns`；恢复定时 warning_seen 后失败。
- N1：激活事务取得准入锁后检查当前时间；固定安全码 `reminder_time_passed` 进入 Action 记录、状态结果与渠道回执，原始异常不外发。014 在本片内同步两表安全码约束。确认材料明确批准时限与已激活任务的补发条件。 回归 `test_approval_at_or_after_due_time_rejects_activation`（到时/过时三值）、`test_activation_rechecks_time_after_waiting_for_admission_lock`，原 U5 提前批准及迟到补执行仍通过；移除激活检查被检出。
- N2：控制与 claim 锁定同一 job，提交后新语句检查 NOT EXISTS attempt；已准入不改状态并明确“发送已准入，未暂停/取消”。实际锁等待覆盖旧快照竞争。 回归 `test_control_after_claim_reports_send_already_admitted`（pause/cancel）、`test_control_waits_for_claim_commit_before_deciding`；分别移除 attempt 守卫、行锁均被检出。
- N3：通知启动恢复在同一条 UPDATE 将 sending 改为 unknown/response_unknown，任务恢复沿用真实原因，不复制 pending。 扩展已有 `test_started_occurrence_recovers_as_unknown_and_blocks_unwarned_immediate_send`，同时断言通知及任务 reason=response_unknown；没有另建重复用例。

**兼容：** B 未发布，应用仍为 schema14；旧候选 v14 的约束及错误短期限不自动改写，也不延长已保存数据或重开群会话。可丢弃试验库重建；需保留时按 README 停服隔离并恢复升级前 v13 配对备份后用更新的014升级。正式 v13→v14、回退和配对恢复由现有维护测试验证，不直接操作 SDK 表。

Python3.11.16 / SDK0.22.3 / httpx2 2.13.1 / 隔离 PostgreSQL。正式入口 FeishuGateway → ChannelService → Application → 真 SDK Runner/Action → GovernedTools → HTTP MockTransport；模型、成员目录、数据库驱动和外部 HTTP 为合成替身。不调用真实服务、不读取公司配置/凭据、不推送/建PR/部署，不混入原目录工作。

| 检查 | 本次证据 |
| --- | --- |
| RED | `/tmp/xiaowei-b-review-red.log`：9 failed；B1/发送截止/N2/N3 到达目标断言。修正 C1 夹具的历史接收范围及 N1 断言顺序后，`/tmp/xiaowei-b-review-contract-red.log`：4 failed，分别复现第二份提醒被门槛作废及到时/过时批准被激活，不算通过 |
| 最小闭环 | 正式入口合同回归6 passed，5.33秒，`/tmp/xiaowei-b-review-contract-green.log`：两次单独批准、unknown 与即时门槛、到时/过时拒绝及状态结果、真实数据库锁等待 |
| 隔离变异 | 六种各单独进程、每种目标测试1 failed（预期）：恢复 min 数据期限、移除 claim 发送截止、恢复定时 warning_seen、移除激活时间检查、移除控制 attempt 守卫、移除控制行锁。`/tmp/xiaowei-b-mutation-{data_ttl,send_deadline,unknown_gate,activation,control,control_lock}.log`；产品源码未被变异插件修改，正式检查不加载插件 |
| 关联回归 | **535 passed，267.63秒**：B专项65项及 Evidence/Session/Action/渠道/通知/文档，`/tmp/xiaowei-b-review-related.log`；原要求保留的四项风险交付和 TTL/迟到用例均通过 |
| 静态检查 | ruff 全仓、六份受影响 Python format、mypy 37源文件及 diff 检查通过 |
| 精确版本复审 | **54a2716 通过本次差异复审**，未发现需修复的可达缺陷；独立关键分支11 passed，11.48秒，另核 v13→v14 保留回执与 SDK 历史1 passed，diff检查通过。审查范围为52f7b0e..54a2716及相关完整调用链，不代替真实U5 |
| 单次完整全套 | **4657 passed, 235 skipped, 89 deselected, 5 warnings in 1594.94s (0:26:34)**；`/tmp/xiaowei-b-review-full.log`。单次默认完整命令在54a2716执行，无变异插件；跳过/未选中项不算能力验收 |

执行命令（均在本隔离工作树；测试前缀为 `SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres PYTHONDONTWRITEBYTECODE=1`）：

- 关联：`.venv/bin/python -m pytest -q -p no:cacheprovider tests/sdk_core/test_{scheduled_tasks,evidence,evidence_scope,r3_evidence,session_policy,channel_store,channel_service,action_lifecycle,action_recovery,action_boundaries,actions_runtime,group_actions,sdk_approval,webhook_notification}.py tests/security/test_docs_command_consistency.py`。
- 单次完整默认全套：`.venv/bin/python -m pytest -q -p no:cacheprovider`。
- 静态：`.venv/bin/ruff check --no-cache .`；`.venv/bin/ruff format --check src/xiaowei/{actions,channel,channel_store,scheduled_tasks,webhook}.py tests/sdk_core/test_scheduled_tasks.py`；`.venv/bin/mypy --cache-dir=/tmp/xiaowei-b-review-mypy src`；`git diff --check`。
- 隔离变异：`/tmp/xiaowei_b_review_mutations.py` 按 `XW_B_MUTATION` 选择一个保护，通过 pytest `-p xiaowei_b_review_mutations` 仅在该测试进程替换方法；data_ttl 单独运行，其余由 `/tmp/run_xiaowei_b_mutations.py` 串行运行，六种目标各1 failed为预期，不计为正式测试通过。

**剩余验收：** 真实 U5（模型日期理解、必要澄清、工具选择、成本及真实平台闭环）未验收。单次完整默认全套本轮已执行，结果见上表；其他旧版分批结果不累计。下一步等待用户审查与真实 U5 环境/费用授权；本轮仅本地提交，C/D 不实施，不推送/建PR/部署。

### 已合入 A 的有效验证

A 候选 `d0db9c35105b14f1dbf615df9d85bcee1ef512bf` 已经 [PR #123](https://github.com/shixian66/xiaowei-agent-sdk/pull/123) 合并为 `9284688bb8907c9968e84d5e49c23e551c53ac58`，树相同；[CI](https://github.com/shixian66/xiaowei-agent-sdk/actions/runs/39597464188) 9/9 首次通过。integration 单次4588 passed/235 skipped/89 deselected，p3-release84 passed，non-SDK1182 passed，security63 passed。该记录只证明 A 的精确版本；不代表 B、真实服务、部署或用户验收。A 的 N1 最终正文统一 mention_safe、审批命令省略及531项本地关联回归已获独立复审，无新依赖或权限扩大。

### 有效证据摘要

原交接曾引用 N1 的本机最终验证记录及全量日志，位于未随 Git 保存的 `.superpowers/sdd/2026-10-20-production-dialogue-answer-repair/`；本工作树无法复核，不提供失效链接或据此新增通过结论。已可查的 #119 合入/CI 证据见上文；下文较早本地统计保留其注明范围，不替代 CI 或真实环境验收，也不能拼作本次默认全量通过。

B1 根因是布局规则误把“长原值收起”扩大成“影响结论的说明也收起”。动态候选过滤/未检查/容量和实际窗口由 `fact_limit_notices` 生成；固定口径复用 ToolPolicy.fact_note_summary，经 Evidence 到各渠道，不新增模型或持久化字段。计划、审计、Grafana 配置、历史引用、结构快照/布局、DDL、告警及数据源配置的语义会影响推断，保留短提醒；完整细则只在详情。普通查询仅在结果确实截断且 LIMIT 被系统调整时提示返回量限制；分页与通用截断合并为一个不完整提醒，原字段继续可核对。原 B2 已撤回，动作状态由消息正文展示，不收集为报告章节；N1 的 MD/HTML `_data_parts` 回归同时验证动作恰好一次、所有附件无动作字段、普通大结果完整交付。原来支持动作进报告的两组变异结论作废；N1 按用户要求不新增隔离变异。

发行验证注意：03c75a3 的默认完整运行是 4494 passed、2 failed、235 skipped、86 deselected，不能视为全量通过。两项发行演练在 PG 启动前失败；独立空白 PG 复现 initdb 的 `No space left on device`，Docker 30G 根盘当时可用为 0。用户本轮另行批准清理清单中 7 个无容器引用的旧测试/Grafana 镜像；已逐项复核、非强制删除，保留全部容器/卷和 Python/uv 基础镜像，清理后约 2.4GiB 可用。最终 N1 候选须在冻结 SHA 重新单次完整运行，结果以本地记录为准。Grafana 与 hostile_plan 的短提醒、原值、来源及反伪造断言保持。

Web 动作提示的可达依据：`action_status_catalog` 只要求 action_id/state，其他状态材料及 result 可因渠道投影容量省略，`Evidence._project` 会标记 truncated；提案和执行结果的必需字段仍是放不下即失败。当前动作策略没有 warnings 字段，生产适配器也未产生动作 warnings。Web 复用服务端 `_view` 的短提醒，对动作一并显示，保持 textContent；Chrome 组件回归使用真实状态策略投影与 `_view` 响应，fetch 是合成替身，不代表开放群动作的 Web 授权入口。查询窗口展示仍统一标注北京时间，不随会话时区切换；这只是显示口径限制，未改变 P2 的查询时区规则。

非阻断建议：文件索引变量已改为 `file_index`；共享密度函数已公开为 `fold_details`。Web 按 JSON 行字节、飞书按排版文字与 SQL 字节决定初始展开，渠道原生排版不同，本轮保留差异；只影响展开密度，不影响必要提醒和完整交付。失败提醒仍使用绑定分项序号，正文索引与文件内标题可辨用途；不解析历史绑定字节猜测角色，不加持久化字段。若用户实收仍难定位失败文件，再单列兼容的用途映射方案。

以下区分最终候选的独立复审/CI 与较早的本地测试记录，结果只覆盖注明的版本、配置与路径，不替代真实环境验收。S3 最终证据见下表的 PR 与 CI 链接；更早的命令、失败与复审过程可用 `git show 7e66718:AGENT_HANDOFF.md` 查阅。旧计划/ADR 只保留其阶段证据，不覆盖当前契约。

| 对应工作 / 版本 | 保留的证据与限制 |
| --- | --- |
| 本轮精确查询与自然回答 / P2 `1c06a89d`、P1 `3a5d62ce` | 两片均完成独立复审；P2 相关 **676 passed**。P1 修订扩展 **478 passed（140.78 秒）**、另补位置 **2 passed**、最终 Chrome **4 passed（5.42 秒）**、文档 **2 passed**、Ruff/mypy 35 文件通过；最终独立检查 **12 passed（8.52 秒）**。首轮 `aa95773` 默认全量 **4446 passed、26 failed、235 skipped、78 deselected（1443.19 秒）**；旧断言/仅读第一文件/测试请求容量已同步，反伪造追查发现的入口位置遗漏已补 RED→GREEN，失败项在扩展回归通过。最终候选未重跑默认全量，不将分次检查拼作全量全绿。作者 36 种、独立审查者 22 种旧 v0 差分输出分别一致；未调用真实模型或公司服务，未部署 |
| 自主调查、普通消息与进度 / 当前计划 | 各片精确独立审查、离线正式入口/隔离 PG/SDK 协议替身、完整检查及真实缺口统一见[计划 §7](docs/superpowers/plans/2026-10-20-agent-autonomy-progress.md#7-当前验证证据与审查输入)。本文件只保留当前状态与下一步；模型能力、真实客户端与生产服务尚未由该证据验证 |
| N1 文案增量 / 基于 `9cb2e37b…` | 仅在既有渲染单点区分 MD 原文展示与 HTML 移除提示，内容处理及绑定字节机制不变；本次定向 **35 passed**、Chrome **5 passed**、文档 **2 passed**，Ruff/mypy/diff 检查通过；本轮用户已确认交付，已复核、已合并（随本 PR 合入 `main` 生效）。没有重跑全量或真实服务，命令与复现见报告计划 §9 |
| MD 审查修复 / `1271517a…` | 内容/正式链 **72 passed**、相关交付/兼容 **162 passed**、Chrome **5 passed**、公共安全门 **63 passed**；Ruff、mypy 34 文件、diff 检查通过。先有失败用例再修复，跨块定义和围栏变异反例已检查；独立复审无阻断，另运行 37 项测试及 1936 组解析探针。本次未重跑全量，不把下一行历史结果复用为该 SHA 全量通过；无真实服务调用，详见报告计划 §9 |
| 原生报告历史候选 / `3ddcb40…` | 默认完整回归 **4329 passed、235 skipped、75 deselected**；报告 Chrome **4 passed**、公共安全门 **63 passed**；ruff、mypy（34 文件）、依赖审计、生产 wheel 入口与 Git 历史敏感信息扫描通过。产品及测试树已独立复审，无遗留阻断；命令与审查覆盖见报告计划 §9。本地结果不代表远端 CI、真实模型或部署通过 |
| 旧工程清理 / 基于 `0bd68f2` 的差异 | 本地默认完整回归 **4270 passed、235 skipped、73 deselected**，含隔离 PostgreSQL 与 Docker 发行用例；恢复公共检查后的定向验证 **92 passed**。ruff、mypy（33 文件）、锁文件与 diff 检查通过，独立复审通过。产品源码及旧回答兼容路径无差异；跳过/未选中项、远端 CI、真实模型与公司服务不计通过 |
| S3 / #114 最终复审、CI 与合入 | [PR #114](https://github.com/shixian66/xiaowei-agent-sdk/pull/114) 记录 Codex 对 `eff89960b71fb660d4d264f3d70f5faba7e2cd22` 独立复审通过；该 head 的 [CI run 39131473170](https://github.com/shixian66/xiaowei-agent-sdk/actions/runs/39131473170) **10 项全部通过**（含 integration、p3-release），`run_attempt=1`，无重跑。以该 head 做 squash 合并为 `6c585d0`，Git 文件树与候选相同；不据此认定真实模型、公司服务或部署已验收 |
| S3 本地定向回归（合入前） | 原审查版本 `73246593` 的三项发现已在 `eff89960` 修复：旧格式不套新预算、v0 截断卡片来源标签、旧审批测试数据与拒绝断言。修复后 16 个受影响文件 **680 passed**，ruff、mypy（33 文件）、diff 检查通过；作者本地未重跑全量与浏览器，最终 CI 另见上一行 |
| S3 修复前全量与浏览器 | SDK/P1B/spike **3755 passed、8 failed**，8 项为旧 spike 格式，修后 spike **107 passed**。其余目录记录 **2123 passed、152 skipped**，另有 11 项失败：10 项日志捕获干扰单独重跑通过，1 项维护探针修后 Docker 升级用例通过。Chrome **21 passed**，3 项被本机 8501 占用阻断；不把这些运行拼成一次全绿 |
| S2 / #113 | `test_answer_s2_protocol.py` **59 passed**，spike **107 passed**；真 SDK + 隔离 PG 验证候选 schema、旧历史和暂停恢复；三组变异分别 3/6/9 项失败。真实 provider 接受情况未由此证明 |
| 引用契约 / #111 | 合入前 SDK/P1B **3612 passed、235 skipped、72 deselected**；其余所选目录 **2099 passed、152 skipped**。仅证明当时规则与改写链，旧强制引用断言不约束 S3 |
| 轮次修复 `d019f40`，随 #110 合入 | SDK/P1B **3785 passed、1 failed、52 skipped、72 deselected**；唯一失败为 Grafana 群重发用例，既有基线整文件同样失败、单跑通过。被拒回答不入历史、Retry-After 封顶及选择器修正均有专项回归；官方 MCP 连接合成端点 |
| 环境选择第一步 / #108 | 修复后 SDK/P1B **3570 passed、234 skipped、72 deselected**；评测计数缺陷另有先失败后通过与变异记录。7 个 StarRocks 工具策略指纹保持，未加入环境硬校验；真实遵守率未评测 |
| 飞书平台门槛 `489ece6`，随 #107 合入 | SDK/P1B **3542 passed、234 skipped、72 deselected**，18 项新增回归在各自修复前失败；真实客户端及文件预览未验收 |
| SQL 原文执行 / #101 | 锁定 sqlglot 30.17.0，可丢弃 StarRocks 4.1.4 真协议 **43 passed**，7 条 asyncmy 非缓冲查询 warning；合成库证据不证明公司 SQL 与资源限制 |
| D7 / #83 | 审查候选 `4905736` 在官方 MCP + 隔离 Grafana/PG 上三个文件 **68 passed、0 skipped**；后续受限 Editor 反例另有记录。不能把早期候选数字当作所有后续改动的复验 |
| C8 / #95 | `6fbebeb` 首轮 C8 **27 passed**、SDK/P1B **3412 passed**；B1 修订后 C8 **32 passed**、受影响文件 **473 passed**。完整回归、Docker 与 wheel 证据属于修订前候选；修订后不可继承为全量通过 |
| P3-A/B / #49、#50 | 本机真实容器验证 A→B/回退、错密钥拒绝、原子 dump 与隔离 restore；核对实际连接数据库与数据差异以避免恢复假阳性。公司故障恢复与用户接受仍未关闭 |

### 仍需保留的限制

- 改写调用自身失败时，锁定 SDK 的脱敏错误包装可能把日志原因归为 `sdk_configuration`，用户仍见 `model_failed`；该诊断缺口仍待处理。
- OpenAI 协议族的 SDK 内置 `conversation_locked` 重试不经过应用的 429/503 策略；Vertex 不触发。选择器组合失败后的子集重试不做语义推导，仍受两次修正额度约束；均为已有记录的边界。
- C8 数据源写前摘要比对不等于原子版本控制，真实环境开放前须按架构核对竞争窗口；Annotation 回读 `limit=100` 边缘情况未关闭。未知插件普通字段中无法识别的凭据风险与写端令牌范围按既有取舍处理，不增加插件白名单。
- 单个大字段超限时预览仍可能没有条目；监控工具返回格式与客户端显示缺口不能用模型编造数据补齐。图形 D4 暂缓，保留已完成的协议核验供明确重启时使用。
- 历史 secret-scan 随机样本不稳定、Grafana 群重发整文件失败及 C8 取消落定偶发失败有记录；单次重跑通过不能核销原因，旧失败记录也不证明当前版本必然复现。
- 显式开启 tracing 的真实字段/接收边界、真实模型任务质量与公司端到端验收仍需对应证据；默认关闭 tracing 的离线检查不能替代这些验证。
