# 小维：当前交接

> 更新：2026-10-21，Asia/Shanghai。本文维护当前代码状态、有效证据、缺口与下一项工作。产品契约见 [ARCHITECTURE.md](ARCHITECTURE.md)，阶段与退出条件见 [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)，使用与运维见 [README.md](README.md) 和 [OPERATIONS.md](deploy/OPERATIONS.md)。

**OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**

## 1. 当前快照

| 项目 | 已核对事实 |
| --- | --- |
| 仓库 | [shixian66/xiaowei-agent-sdk](https://github.com/shixian66/xiaowei-agent-sdk) |
| 产品代码基线 | 当前监控补查候选从最新 `main@9284688bb8907c9968e84d5e49c23e551c53ac58`（#123）独立开始；工作树 `monitoring-read-recovery/agent-SDK`、分支 `codex/monitoring-read-recovery`。通知/定时任务工作区未修改 |
| 源码与检查 | 当前源码仅保留 `src/xiaowei`；旧工程及专属测试、Compose、迁移入口、`compose-smoke` CI 和 Alembic 依赖已移除。保留产品测试、公共检查与旧回答兼容路径；旧工程历史可从 Git 查回 |
| 当前存储与回答 | 沿用主线应用 schema v13，无数据库迁移；SDK 表、可信 answer_version=3、2/Legacy 与分项交付不变。失败原因闭集新增 `object_missing`，旧镜像读取新原因不保证兼容 |
| 锁定依赖 | SDK `0.22.3`、Lark Channel `1.4.0`、MCP `2.2.0`、sqlglot `30.17.0`、`nh3 0.3.7`、`tinycss2 1.5.1`、Pillow `13.0.0`、certifi `2026.10.19`（后两项为严格审计发现的既有漏洞定向升级） |
| 正式入口 | `xiaowei serve`、`config check`、`model check`、`storage init/upgrade/cleanup`、`requests resend`；命令与环境准备以 README 为准 |
| 最近有记录的公司部署 | 用户本次提供线上镜像 `xiaowei:143c4c8641be636c41c130329ce1639a36b59c27` 及新会话巡检日志；这是用户提供的部署证据，未独立核验当前服务器状态。本候选未部署 |
| 当前验收结论 | 产品 `0da5d9f` 独立复审通过，两项日志P2关闭。本轮F1/F2仅修订文档，基线回退路径及成功对照4项通过；本地是否满足开PR条件只取本轮最终HEAD的全仓、发行和静态检查实际结果，记录方式见§3，不继承旧回归数字 |
| 当前任务授权 | 仅本地处理F1/F2文档意见并完成最终HEAD验证；不改schema、不加兼容层、不改旧镜像行为或定时任务工作区。暂不推送、不开PR、不合并、不部署；本地门槛通过后仍由用户决定下一步。未授权真实模型、公司源或真实消息发送 |

原工作目录的 ARCHITECTURE 差异与未跟踪计划/公司配置保留，未复制到本工作树；本次未读取公司配置凭据。旧通知/定时任务和回答修复记录保留为历史证据，不能作为本监控候选的范围扩展授权。

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
- 通知及定时任务的方向与待审权限增量见 [ARCHITECTURE](ARCHITECTURE.md#scheduled-agent-tasks)：多个目的群不扩大操作入口，任务独立于原聊天，创建及每次执行复核权限，生产写不能继承周期批准。A 已有本地候选与离线证据，B–D 未实现。

## 3. 当前计划与下一项工作

### 当前独立候选：监控补查修复

本轮接收针对 `9284688..0dda020` 的审查意见，按共同根因处理：

- **F1（文档残留）成立并修订：** 删除本文“子集重试仍受两次修正额度约束”的半句，保留不推导选择器子集语义的边界。未修改实际配额、治理或测试。
- **F2（已接受的回退限制）成立并修订：** 使用基线 `9284688` 的实际模块、SDK及隔离PostgreSQL复现；新原因在旧账本解析时拒绝，暂停未消费、两次批准及到期检查均零写入；历史读取/重发在 `_saved_answer` 拒绝，发送替身零调用，记录原值保留。含正常批准、旧原因可解析对照共4项通过；当前候选的账本/回答解析能接受新原因。操作员说明已补入 [OPERATIONS](deploy/OPERATIONS.md#监控补查修订的回退限制)，按用户决定只补文档，不改schema或增加兼容层。
- **F2细节纠正：** 基线内部由 `app.py` 的账本 `ValidationError` 映射为 `model_failed`，但正式渠道 `channel.py` 的 `TurnError → _approval_failure` 路径会对仍待批的动作返回 `action_not_approvable`。实测内部日志与渠道请求失败码分别如此；审查所述“最终只得到笼统轮次失败而非action_not_approvable”不成立，不据此增加错误处理。
- **Docker空间及清理：** 用户提供的审查证据确认此前VM磁盘用满，`initdb` 的 `pg_wal` 创建报No space left on device；基线/候选同样失败。本轮只读盘点已变为29.4GB总量、16.0GB已用、11.8GB可用（58%）。未执行空间清理、未请求删除授权；当前建议不删除已有资源。镜像/容器/卷逐项清单含大小、运行引用和推测来源，保存于本轮本地验证目录。无引用不代表无数据或已获删除授权。

为满足“最终HEAD全仓回归”，先选择性提交上述文档并冻结HEAD，再执行以下检查，不在检查后为写入自身SHA追加提交。**最终SHA、实际通过/失败/跳过数及是否具备开PR条件，按本轮最终交付回复与本地 `final-report.md` / JUnit 原始记录核对，不预填通过。** 本轮本地验证目录：`/var/folders/r1/9lhtpjbj4qdbl4g3py9myqs40000gn/T/xw-monitoring-f1f2-ti8k8vhw/`；原始运行记录不作为发布包或业务数据提交。

```sh
SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider --tb=short --junitxml="$XW_REVIEW_OUTPUT/full.xml"
SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/deployment/test_release.py tests/deployment/test_compose.py tests/deployment/test_maintenance.py tests/deployment/test_release_workflow.py -q -p no:cacheprovider --tb=short --junitxml="$XW_REVIEW_OUTPUT/release.xml"
.venv/bin/ruff check --no-cache .
.venv/bin/mypy src
git diff --check
```

`XW_REVIEW_OUTPUT` 仅为上述本地验证目录，不是产品配置。测试使用现有隔离实例并保留harness标识核对，不运行共享实例的启动/停止/重建/清理命令；发行测试仅使用其本轮新建的隔离项目和资源。既有资源不做空间回收；若再次空间不足，先给用户列出具体可回收项再确认，不使用prune、不降低门槛。

官方Alertmanager/Grafana二进制协议、本轮真实模型自主补证及结论限定、公司监控源、飞书仍未验证；脚本回放不是能力验收。

下面保留产品候选及早先验证的版本记录；不能将其数字当成本轮最终HEAD的结果。

用户授权在 `monitoring-read-recovery/agent-SDK`、分支 `codex/monitoring-read-recovery` 从最新 `main@9284688` 独立实施，不动定时任务工作区；只做离线验证与独立审查，不合并、不部署。下面通知/定时任务段是主线已有交接，不能据此扩展本候选范围。

本候选移除三类源内共享两次修正配额，保留总预算和失败请求去重；静默列表可修正读取错误不再停止整源；Grafana 当前 UID 明确 NotFound 只停止同一对象的摘要/面板重发。新错误原因 `object_missing` 是失败反馈契约的窄增量，旧镜像读取该原因不保证兼容；成功 Evidence、权限、写审批、Session 数据边界、报告交付与 SDK Loop 不变。方案、回归与真实验证边界见[监控评测说明](docs/monitoring-reliability-evaluation.md#监控补查修复候选2026-10-21)。

产品冻结于 `0da5d9f0f64bec4c8056e7fca7ea57d22160730a`，独立审查及增量复审通过。原始供应商调用ID日志泄露、未知目标路由拒绝时摘要编码异常两项P2均已用真实路径复现并修复：日志只输出固定摘要，业务ID不变，不增加校验或策略。最终复审独立验证了SDK实验的两个恢复参数，确认失败去重、正确补查、整源停止、失败交付及预算均保留；早先独立复验12项及最终日志反例3项也通过，各轮有重叠，不相加。

离线验证按版本记录，不自动晋级为最终SHA全套通过：

- 基线关联123 passed、1 skipped、1 deselected；8个新核心反例先失败后通过。
- `a662eb6` 关联回归298 passed、28 skipped、4 deselected；跳过为未配置的官方MCP/服务，浏览器未选。最终摘要编码变更后治理/日志89 passed；旧SDK实验及源失败/日志30 passed、1 skipped、1 deselected。最后两组有重叠。
- 日志修订前启动的完整回归：4599 passed、4 failed、235 skipped、89 deselected、5项预期网络拒绝警告，1256.72秒。它不是最终SHA全套验证。其中SDK实验旧 `promql_pending` 断言失败已改为可观察恢复行为，模块9 passed，独立复验2 passed。源码、全部测试和脚本已搜索，无已删字段的实际依赖。
- 剩余3项均为未改动的历史发行维护：`test_different_image_upgrade_rollback_and_paired_restore`（487行）、`test_container_migration_v4_to_v5_to_v6_and_old_program_refusal`（741行）、`test_feishu_users_upgrade_rollback_restores_the_config_the_old_image_reads`（993行），都在初始 `postgres --wait` 报容器 unhealthy，尚未进入应用路径。第一项已单独复现（15.15秒）；当时未定位具体容器根因；本轮审查补充及空间变化见上文，不据此直接核销三项失败，须由最终HEAD实测确认。
- 最终产品 `ruff check --no-cache .`、`mypy src`（36文件）、`git diff --check` 通过。成功Evidence、权限、SDK Loop、Session及交付源码没有变更，失败原因新增及固定标签除外；应用schema仍为13。

新增解释样例共11场景14轮；不把脚本回放算为诊断能力。未调用真实模型、公司源或飞书，未推送、开PR、合并或部署。下一步是用户审阅本候选后决定PR/CI安排，发行维护和真实模型自主补证/结论质量仍须分别验证；不据此关闭线上整体安全结论问题。

### 通知与定时任务：A 审查修复离线通过

[设计 v1.4](docs/superpowers/specs/2026-10-21-scheduled-agent-tasks.md)维护技术依据与固定 U1–U7；[A–D 计划 v1.4](docs/superpowers/plans/2026-10-21-scheduled-agent-tasks.md)维护切片验收。沿用批准的规范化定义、Evidence 依赖/期限和创建者＋原群权限口径。A 即时多机器人通知 → B 一次性固定提醒 → C 通用独立 Agent 周期任务 → D Node 核心巡检。

A 已增加窄 notify effect，独立发送许可不扩展数据库/生产写权限。后续源码缺口：原 Action 是短期单次批准，B 须验证激活独立 job 授权；现有 Prometheus MCP 文本丢失结构化信息，D 不能猜解析，届时再补必要的受治理结构化读取。

A 已实现：静态多机器人、安全凭据引用、个人发送许可、notify 治理、全可见 Evidence/接收范围复核、显式事实纯文本渲染、先落库后 POST 与独立回执。发送权限与安全群回执读取分开；普通查询仍完整交付，外发不自动补上未选择结果。unknown/回执暂不可核验均告知重复风险，不自动重发；已 accepted 不因后续模型/Session 失败变成未发。

对 `d42b999` 的三项阻断及后续 P2 已修复并独立复审：正文改由 blocks/facts 渲染；容量提示保留通知结果/风险；隐藏审批恢复轮通知，等待批准及普通请求中仍可批准的材料由代码提示完成后另提要求；未知门槛按原群＋机器人共享。A 超限整条不发，不实现自动裁剪；机器人可显式登记实际 SDK 执行请求证据接收范围，不增加写权限。本次止于 A 修复；B 接续时仍先验证 ActionBinding 激活独立授权、定义 Evidence 依赖/TTL 和直接暂停/取消，不同时实现 C/D。真实 U1–U4 仍须先获模型 Profile、费用及测试机器人授权，不得把离线通过当作真实能力、群内实收或生产开放。

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

### 2026-10-21 A 通知候选验证

当前 N1 修复差异为 `fb47d46303fd31a1b022fa7e8e22946bc3544f8e..105c26179133f1ca3921f1d33893b1ede43a080a`，分支 `codex/webhook-notifications`。N1 已复核成立：本地 Webhook 绕过普通飞书发送的 `mention_safe`，SQL/事实行、模型说明及明确文字均可带原始 at 标签。根因在纯文本外发边界；现仅在 `webhook.py` 的最终正文归一化处统一中和，再构造请求、检查最终20000字节和计算去重摘要，不在各事实行重复处理。其他外发链已检查：Webhook notices 包含在同一最终正文中；应用机器人容量/预览 notices 最终经过既有 `LarkTransport.send`，未新增重复防护。

两项非阻断建议均采纳：通知渲染省略目的群不可用的 `approval_command` metadata，保留 Action ID/状态/影响及原 Evidence/原群审批材料；`test_actions_runtime.py` 原245行按 ruff format 整理。无依赖、迁移、权限或审批契约变化；五项已批准取舍保持不变，不开放主动 @所有人或成员。

| 本轮检查 | 实际结果与范围 |
| --- | --- |
| 修复前 RED | 新增纯文字、事实、模型说明和审批命令4项均失败，见 `/tmp/xiaowei-webhook-n1-red.log`。首次修复回归92通过/1失败，事实用例的驱动固定返回 sales 而没有 note；已改为实际含标签的结构化行，保留行断言，日志 `/tmp/xiaowei-webhook-n1-green.log` |
| 隔离变异 | 临时 pytest 插件仅在该进程把 `webhook.mention_safe` 换为恒等函数，三种来源及中和后容量准入4项全部失败；源码不变。日志 `/tmp/xiaowei-webhook-n1-mutation.log`，插件 `/tmp/xiaowei_n1_unguarded.py` |
| 最终相关回归 | **531 passed**，134.13秒；命令 `.venv/bin/python -m pytest -q -p no:cacheprovider tests/sdk_core/test_{webhook_notification,feishu_render,feishu,feishu_format,feishu_reports,delivery_parts,actions_runtime,group_actions,sdk_approval}.py`。真实群入口/SDK/治理/隔离PG/HTTP替身验证两类成员标签中和、普通文字与换行、换 call_id 只发一次、摘要匹配实际HTTP正文，以及中和扩张超限零HTTP/零attempt。日志 `/tmp/xiaowei-webhook-n1-related.log` |
| 静态与文档 | ruff全仓、mypy 36源文件、3份受影响Python文件的ruff format检查、diff检查、文档命令2项通过；gitleaks全历史无泄漏，规则未变，日志 `/tmp/xiaowei-webhook-n1-gitleaks.log` |
| 独立复审 | 对 `fb47d46..105c26179133f1ca3921f1d33893b1ede43a080a` 复审通过，无阻断。审查者另重跑 **9 passed**，5.29秒，覆盖三来源、命令省略、扩张容量、普通换行、签名、跨成员unknown门槛及正式审批恢复/另轮通知。确认 notices 经过相同最终出口，无独立分支，不额外造测试或重复防护；未以旧候选审查替代 |

本轮未运行单次完整产品全套、发行容器或真实服务，不能把531项与旧分批检查相加称为全套通过。SDK为0.22.3，Python为3.11.16；PG命令设置 `SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres`、`PYTHONDONTWRITEBYTECODE=1`，正式测试不加载变异插件。旧发送记录不改写，凭据/原目录已有修改不读取或混入。真实模型自主能力、飞书实际标签解释/群内表现及公司环境仍未验证；本次只证明离线请求正文及治理边界。

以下保留上一候选的有效证据，均不是本轮 N1 修复后的完整检查：实现起点为批准设计 `71398cf8e7f076e6bc3c840a6566ca49a7709f00`，上一修复差异为 `d42b999a254b0c39383c2a02a5a90b062236eafb..79e03aba214389a90cf2e359d86d1683c13a3c94`；主修复 `af05350`，增量修复 `79e03ab`。文档为 v1.4；独立复审分别检查并关闭当时的补充 P2，另重跑渲染5项及正式入口1项。使用独立 `.venv` 和隔离 PostgreSQL，无真实模型/飞书/公司源调用。

上一候选分批验证（未重跑单次完整产品全套）：

| 检查 | 结果与版本 |
| --- | --- |
| 通知/容量/正式审批最小闭环 | 首轮88 passed；后续补齐非活动请求检查及普通 pending 状态材料回归，纳入以下检查 |
| 关联回归 | **929 passed**，198.68秒；17模块：SDK core 的 webhook_notification、actions_runtime、feishu_render、channel_service、channel_store、feishu、feishu_reports、delivery_parts、inline_delivery、group_actions、governance、evidence、evidence_scope、runtime、session_policy、session_warning_render，以及 security/docs_command_consistency。普通提示最后移位后另跑 actions_runtime/channel_service **51 passed**。测试命令均为 `.venv/bin/python -m pytest -q -p no:cacheprovider` 加对应 `tests/.../test_*.py`，设置下述隔离 PG URL |
| 独立复审补充 P2 的 RED→GREEN | `test_actions_runtime.py -k notify_after_approval` 在普通 completed 动作状态材料缺提示时先失败；修复后 `test_actions_runtime.py test_channel_service.py test_group_actions.py test_sdk_approval.py` **124 passed**，81.50秒，覆盖最新 `79e03ab` |
| 发行 | `af05350` 的 `tests/deployment/test_{release,compose,maintenance,release_workflow}.py` **84 passed**，397.96秒；最新增量只改上述提示条件和回归，重新离线构建 wheel、无dev测试运行器的临时生产环境两个 CLI 入口及013迁移检查通过。非公司部署 |
| 静态、秘密、文档 | 最后代码版 ruff 全仓、mypy 36源文件、diff检查通过；gitleaks全历史无泄漏，配置/allowlist未变；130个本地路径及定时章节锚点通过。依赖未再变更，沿用下表严格审计 |

本次日志为 `/tmp/xiaowei-claude-fixes-connected-final.log`、`/tmp/xiaowei-prepared-material-{red,green}.log`、`/tmp/xiaowei-claude-fixes-release.log` 和 `/tmp/xiaowei-claude-fixes-gitleaks-final.log`，均是本机临时证据，不是远端 CI 或永久附件。关联首轮928通过/1失败为新增夹具误用 SDK 上下文，后续已用公开 ToolContext 修正；误移的旧恢复断言也原位保留并通过，没有删除断言或放宽重发行为。

下表为先前候选 `18159b0` / `d42b999` 的有效历史证据，不能代替本次产品源码修复后的验证。`0945083` 仅同步旧测试预期并补直接 v12→v13 升级。

| 检查 | 实际结果与边界 |
| --- | --- |
| 正式群事件→runtime/Application→真 SDK→治理→隔离 PG→HTTP 替身 | 通知专项 **44 passed**；包括 U1–U4、并发/新 call_id、防重、签名、接收工具/源、无权用户/渠道、历史回放、未选数据不外发、未展示风险禁重发、取消/落库/Session 故障与凭据轮换。脚本模型不证明自然语言自主能力 |
| 产品完整回归（排除单独执行的 deployment） | 在 `18159b0` 执行 `python -m pytest -q -p no:cacheprovider --ignore=tests/deployment`：**4470 passed、13 failed、235 skipped、89 deselected**。13项全为当前版本/新回执字段的旧预期；不是全套通过 |
| 失败修正后的受影响模块 | `python -m pytest -q -p no:cacheprovider tests/sdk_core/test_storage_v2.py tests/sdk_core/test_runtime.py tests/sdk_core/test_evidence.py tests/sdk_core/test_webhook_notification.py`：**290 passed**，包含全部13个原失败及新增直接 v12→v13 保留历史测试。仅改测试，产品源码与上述完整运行相同；据此记录分批覆盖通过，未重跑单次完整全绿 |
| 发行必需检查 | `python -m pytest -q -x -p no:cacheprovider tests/deployment/test_release.py tests/deployment/test_compose.py tests/deployment/test_maintenance.py tests/deployment/test_release_workflow.py`：**84 passed**，真实本机隔离容器、升级/回退及配套备份恢复。不是公司部署 |
| 依赖与正式包 | 严格 `pip-audit --strict` 通过；审计发现的既有 Pillow/certifi 漏洞仅定向升级到13.0.0/2026.10.19，Python 3.11兼容，图片/CLI相关145项通过。最新 wheel 在无dev依赖环境的 `xiaowei --help`、`python -m xiaowei --help` 通过，含新迁移，不含测试/公司配置 |
| 静态与秘密 | `ruff check --no-cache .`、`mypy --cache-dir=/tmp/xiaowei-webhook-mypy src`（36文件）、`git diff --check` 通过。gitleaks8.30.0官方校验、自测及全历史扫描通过（产品候选676提交，无泄漏）；未弱化 allowlist。文档命令2项、139个本地路径及定时章节锚点通过，未验证外网链接/全仓锚点 |

上述 PG 命令均设置隔离 `SDK_TEST_POSTGRES_URL`，禁字节码并关闭pytest缓存。失败完整运行和后续分批通过分开保留；本机日志 `/tmp/xiaowei-webhook-final-product-suite.log`、`/tmp/xiaowei-webhook-post-suite-fixes.log`、`/tmp/xiaowei-webhook-p3-after-cleanup.log` 是临时证据，不能假称远端 CI 或永久附件。发行早期失败除旧 schema 夹具/预期外，曾由 Docker 30G 数据盘耗尽导致；捕获 initdb 的 `No space left on device`。用户明确批准后，逐个复核并非强制清理清单内78个无标签/无容器引用旧构建镜像，保留全部有标签镜像、容器和卷，恢复约1.8GiB再跑发行全套通过。

早期独立审查对 `1dfbc62` 曾发现未选择结果自动外发、共享会话被个人发送权阻断、回执读取异常，修复后对 `18159b0` 增量再审通过；该结论被 Claude 对 `d42b999` 发现的三项具体阻断补充，不能当作当前交付通过。正文/容量丢失已先复现；审批恢复原请求仍 paused，不能直接放开，因为原请求 sent 仅证明审批材料的交付。用户明确选择恢复轮不通知及群共享门槛，产品边界唯一见 ARCHITECTURE。旧“限定用户原文”机制仍不恢复；B/C 须验证完整定义依赖、源归属和最早期限。

真实 U1–U4 的意图选择、答案质量、实际工具数/耗时/用量、机器人关键词/IP/签名/限流、群成员实收及部署均未验证。B–D未实现；长期任务 ActionBinding、清理/权限/TTL 与 Node API 结构化数据仍按各片门槛接续，不因 A 完成豁免。

既有 #120 的 P4 记录曾保存在原工作树 `.superpowers/sdd/2026-10-20-production-dialogue-answer-repair/p4-results.md`，当前工作树不含该本机文件；源码差异可从 `6918044` 查回，不能据未随 Git 保存的材料新增实测结论。该切片只更改测试、采集脚本和相关文档；不关闭 A1、真实 P4 或客户端验收。采集器的首个正文回执指标不等于用户实际看见答案；完整交付依赖全部绑定分项确认。有限 PromQL 与 SQLite 夹具的能力限制单列，不能凭脚本模型给自然语言任务打通过分。

P2/P1 的 [PR #119](https://github.com/shixian66/xiaowei-agent-sdk/pull/119) 由候选 `425a52c13bfc6e1bc9cf441e314db6435885ca98` squash 合入 `821ee8d9d89c1a28dda5c9b2c97d8506a3e0407d`，文件树一致。[CI 39456492469](https://github.com/shixian66/xiaowei-agent-sdk/actions/runs/39456492469) 的 9 项检查全部通过、attempt=1：integration **4500 passed、235 skipped、87 deselected**；p3-release **84 passed**。这是该合入版本证据，不自动当作本次 P4 候选的全量结果。

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
- OpenAI 协议族的 SDK 内置 `conversation_locked` 重试不经过应用的 429/503 策略；Vertex 不触发。选择器组合失败后的子集重试不做语义推导；均为已有记录的边界。
- C8 数据源写前摘要比对不等于原子版本控制，真实环境开放前须按架构核对竞争窗口；Annotation 回读 `limit=100` 边缘情况未关闭。未知插件普通字段中无法识别的凭据风险与写端令牌范围按既有取舍处理，不增加插件白名单。
- 单个大字段超限时预览仍可能没有条目；监控工具返回格式与客户端显示缺口不能用模型编造数据补齐。图形 D4 暂缓，保留已完成的协议核验供明确重启时使用。
- 历史 secret-scan 随机样本不稳定、Grafana 群重发整文件失败及 C8 取消落定偶发失败有记录；单次重跑通过不能核销原因，旧失败记录也不证明当前版本必然复现。
- 显式开启 tracing 的真实字段/接收边界、真实模型任务质量与公司端到端验收仍需对应证据；默认关闭 tracing 的离线检查不能替代这些验证。
