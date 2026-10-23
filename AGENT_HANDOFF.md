# 小维：当前交接

> 更新：2026-10-23，Asia/Shanghai。本文维护当前代码状态、有效证据、缺口与下一项工作。产品契约见 [ARCHITECTURE.md](ARCHITECTURE.md)，阶段与退出条件见 [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)，使用与运维见 [README.md](README.md) 和 [OPERATIONS.md](deploy/OPERATIONS.md)。

**OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**

## 1. 当前快照

| 项目 | 已核对事实 |
| --- | --- |
| 仓库 | [shixian66/xiaowei-agent-sdk](https://github.com/shixian66/xiaowei-agent-sdk) |
| 产品代码基线 | `main@7a790c36b9be29080fdc896a48a6ed02aa79d332`（含 #128、#129、#130）；当前分支 `codex/ci-small-optimization` 复用已隔离工作树 `/Users/kloenguyen/.codex/worktrees/manual-starrocks-security/agent-SDK`，原目录及其他任务修改保留 |
| 源码与检查 | 当前源码仅保留 `src/xiaowei`；旧工程及专属测试、Compose、迁移入口、`compose-smoke` CI 和 Alembic 依赖已移除。保留产品测试、公共检查与旧回答兼容路径；旧工程历史可从 Git 查回 |
| 当前存储与回答 | 应用 schema v15 不变；C 扩展已有 job、逐次 run/通知关联；014 已发布不改，015 显式升级。当前本地新回答保存 v4，旧 v2/v3/Legacy 与已绑定交付保持原语义，SDK 表不改 |
| 锁定依赖 | SDK `0.22.3`、Lark Channel `1.4.0`、MCP `2.2.0`、sqlglot `30.17.0`、`nh3 0.3.7`、`tinycss2 1.5.1`、Pillow `13.0.0`、certifi `2026.10.19`（后两项为严格审计发现的既有漏洞定向升级） |
| 正式入口 | `xiaowei serve`、`config check`、`model check`、`storage init/upgrade/cleanup`、`requests resend`；命令与环境准备以 README 为准 |
| 最近有记录的公司部署 | 主线交接记录含用户提供的 `xiaowei:143c4c8641be636c41c130329ce1639a36b59c27` 镜像及巡检日志；未独立核验当前服务器状态。本次未部署 |
| 当前验收结论 | A/B/C 与代理已合入主线；S0/S2 已通过独立复审。S1 首版 c44bbdf 全套通过；Claude 对 d29c2f0（受测 af797c0）确认 P1/P2(b) 通过，P2(a) 附件回答优先级回归已在 b08c042 本地修复并定向验证，用户确认 ce52831 复审通过；真实模型、平台输入/识图、客户端与 U5/U6/U7 仍未验收，D 暂停且未实现。精确版本和证据见 §5 |
| 当前任务授权 | 用户批准 CI 小范围优化：README 仅维护命令并引用 AGENTS §6、发行测试去重、耗时统计和相应策略/交接文档；本地按计划定向检查、推送 PR 后验证最终 CI。不增加并行、缓存或路径筛选，不合并、不打包、不部署 |

原工作目录的 ARCHITECTURE 差异与未跟踪计划/公司配置保留，未复制到本工作树；本次未读取公司配置凭据。旧监控和回答修复记录保留为注明版本的证据，不作为本轮输入或展示范围扩展授权。

本文件的状态以表中的核对基线为限。接手者先核对实际 HEAD、工作区及用户新指令；合入、独立审查、离线测试、真实运行、部署与用户接受分别记录，不互相替代。

## 2. 已确定的产品边界

本文只索引仍影响接续工作的决定，不另维护一套架构契约：

- 一个业务 Agent、SDK 唯一 Loop、SDK Session + PostgreSQL、受治理执行与四种数据边界，见 [ARCHITECTURE §3–§9](ARCHITECTURE.md)。StarRocks 继续本地 Adapter 直连，数据库 MCP 暂缓。
- 普通消息由单 Agent 判断是否查询；显式诊断入口才确定性隐藏并拒绝实际查询。环境选择目前依靠会话历史与随包 Skill，执行层没有“用户选定环境”硬校验；接受风险与第二步触发条件见 [R7](ARCHITECTURE.md#r7-targets)。
- SQLGuard 检查业务 SQL 原文，物理表必须带库名；唯一候选可由 Agent 补全后重试，不能把旧的自动补库或 AST 重打印恢复为当前行为。查询资源边界和已接受的视图、审计数据范围见 [§6](ARCHITECTURE.md#6-只读查询保护)。
- 当前 v4 选择所需事实，不自动补交未引用结果；v2/v3 保留保存语义，均按[长期交付规则](ARCHITECTURE.md#user-delivery-and-history)管理，不恢复旧 `uncited_query`。权限、Evidence、未完成执行和存储边界保留；模型文字真实性仍须任务评测，结构正确不代表结论正确。
- 进度的单聊/群聊差异延迟、仅真实变化更新、正常收尾及异常陈旧已由用户确认，详见[进度契约](ARCHITECTURE.md#progress-observation)；P1 已有离线实现，当前证据见计划 §7。普通消息的区域身份及正文/明细分工见[普通消息契约](ARCHITECTURE.md#ordinary-feishu-expression)，不能由 HTML/MD 报告完成推定达标。
- 监控写仅限已批准动作，使用 SDK 原生审批暂停/恢复及既有 Action 治理；生产开放仍须满足对应源和群审批门槛。Grafana 数据源密码私聊命令尚未实现，目前由管理员在 Grafana UI 填写。见 [监控边界](ARCHITECTURE.md#monitoring-mcp)。
- 内网 Web 无登录、访问者共用操作者权限；目标账号、网络、Grafana 插件/写端及数据源更新竞争窗口等已接受取舍按架构执行。
- 通知及定时任务的方向与待审权限增量见 [ARCHITECTURE](ARCHITECTURE.md#scheduled-agent-tasks)：多个目的群不扩大操作入口，任务独立于原聊天，创建及每次执行复核权限，生产写不能继承周期批准。A/B/C 与自建代理适配已合入主线；群输入已通过独立复审，S1 展示已完成本地实现、离线验证及独立复审，用户授权 PR 集成，D 暂停且未实现。

## 3. 当前计划与下一项工作

### CI 小范围优化（PR #131）

[批准计划](docs/superpowers/plans/2026-10-23-ci-small-optimization.md)已落实为发行目录归属调整及耗时统计，README 只保留命令和权威规则引用。现有九个 CI job 名称、触发、权限与失败退出语义保留，产品代码和业务测试断言未改。基线及最终 head、独立审查、远端 pytest/job/workflow 耗时、排队时间和慢用例集中记录在 [PR #131 的运行证据](https://github.com/shixian66/xiaowei-agent-sdk/pull/131)，避免为追记 CI 结果再提交文档触发全量。

本地证据：三组 collect-only 使用同一环境并设置仓库固定的 SDK_TEST_POSTGRES_URL，只收集不执行；原默认集合 **5245 = integration 5161 + 发行目录 84**，按用例 ID 比较无遗漏、无额外、无交集，前两组均为 96 deselected。并行收集总墙钟 **4.554 秒**；临时明细在 `/tmp/xw-ci-small-optimization/collection-summary.json` 和同目录 `.ids` 文件。CI 命令改动先触发策略检查 5 项预期失败，同步受审命令/摘要后相关策略和文档检查 **30 passed，0.18 秒**；相关 Ruff 和 diff 检查通过。没有重跑本地业务全量。

用户提供的历史本地实测为 **2028 秒（33分48秒）、4900 个用例**，未提供提交 SHA；只作历史参照，不作同版本性能对比。远端历史与收益估算见计划，最终收益仅按 PR 的实测记录报告，不把各并行 job 耗时相加为等待时间。

**剩余慢点／下一步：** integration 当前按单进程顺序执行测试；本轮采集 `--durations=30`，主要慢点及其耗时见上述 PR。下一步主要候选是 **pytest-xdist**：结合这些数据，从少量 worker 验证耗时、共享资源竞争和稳定性，再决定是否采用及 worker 数量。本轮未安装或启用并行；当前测试运行不能证明并行适配、真实模型或公司服务可用。用户复核最终 PR 后另行决定合并。

### 手动 StarRocks 安全巡检（2026-10-23，离线候选）

[当前计划](docs/superpowers/plans/2026-10-22-manual-starrocks-security-inspection.md)在 `codex/manual-starrocks-security`、`/Users/kloenguyen/.codex/worktrees/manual-starrocks-security/agent-SDK`，原始基线 `1a92c58`，本次已 rebase 至 `main@b7d033caa36b47cae1d9cfab5bc099c0f46911b4`，无冲突；与「定时任务」对话工作树隔离。用户确认计划 blob 340af95 复审通过并授权输入/计数及 A1b。现已实现一个只读 inspect_audit_security、短 database_security Skill、全窗聚合/粗筛两条 SQL、纯分析与既有 SDK/治理/Evidence/报告接线，不改表结构。本次按用户批准移除将未开发/未验收误作禁止的自动工具拒绝及未登记能力提示词禁令；复用既有配置、权限和自动执行治理，保留 Skill 的不自动补取原文/口令/错误全文。产品规则唯一见 [ARCHITECTURE §6](ARCHITECTURE.md#6-只读查询保护)。

运行时完整性只看候选是否截断及是否全部可判定；全窗超长数单列，非候选超长不触发 truncated。OR 夹注释、DELETE 等已知缺口保留；两条查询不承诺同一快照。样本两列多值特征转为文本以复用 Web 事实表，内部仍按列表/集合去重。新增 security 真实模型采集套件复用既有入口与同一 S1–S5，默认各一次；本地只用脚本模型，不是 Agent 自主能力验收。

当前验证分版本记录：3988aefd92b45e2e68b7907c583e489a3fa3e08e 的 [CI 39834325687](https://github.com/shixian66/xiaowei-agent-sdk/actions/runs/39834325687) 9/9 通过，完整套件 4900 passed、235 skipped、89 deselected、5 warnings；这是修复前版本的证据。早期三项失败中的两项容量夹具已校准且未放宽生产限额/拒绝断言，SDK 暂停 spike 的早期偶发根因仍未证实。

3988aef 的后续独立复审发现唯一阻断：SQLGlot 30.17.0 将 INSERT 后的优化器提示词分成 HINT，导致 INSERT /*+ SET_VAR(a=1) */ OVERWRITE 漏判。已先复现精判和粗筛包含关系两项失败，再仅在 _operations 取首/次关键字前过滤 HINT；保留仅含 Hint 的正常反例，避免空列表。共享 SIGNAL_CASES 同时覆盖精判与粗筛包含关系，公司清单已补同一条字面量。局部分析/采集及文档检查 98 项、相关 lint、mypy 38 文件和 diff 检查通过；修复代码 cb9dca32601a5ca2aeef7301732dc73af52f9077 已仅针对相对 3988aef 的差异独立复核通过，审查者独立复现旧版两项失败、当前两份相关测试 96 项通过，未发现新增阻断。3988aef 发行包不含此修复，不能当作修复后的验收包；该局部修复已随 #128 合入；本次新增两项调整仍须按最终 HEAD 独立复审，不合并、不打包、不部署。

独立审查 5b51059 发现 S4 字节裁剪会丢掉本来放得下的晚出现操作代表；已沿正式 SDK/Evidence 入口先复现再修复。分析结果保持代表优先顺序，让既有字节裁剪保留各类样本，不增加容量或第二套选择器。修复版本 a451f573836df6c7be247035ab2bbd3cf7dc0057 已经独立复核，无其他阻断项；新增行数/字节两个配置的反例，作者与审查者分别运行分析/采集 93 项、正式入口 21 项全部通过，lint/type/diff 检查通过。

首次远端 CI 另发现两项基线问题：opentelemetry-api 1.45.0 命中 CVE-2026-114603；密钥扫描自测将随机 Base64 的 +/ 删除，可能不足 40 字符。已仅将该传递依赖补丁更新为 1.45.1（[官方修复](https://github.com/open-telemetry/opentelemetry-python/releases/tag/v1.45.1)），strict pip-audit 无已知漏洞；自测仅删除换行，保留退出码 1 和全历史扫描要求。合成输入复现旧长度 20、新长度 40；同步受审 workflow 摘要，策略 28 项通过；补丁 33b265b46d5372960d19d69b72fa1453d20517f8 独立复核通过，审查者用 gitleaks 8.30.0 执行两段实际自测均检出样本。补丁后相关回归 214 项、安全 gate 63 项、lint/type 均通过；不把基线问题说成巡检新引入漏洞。

公司验收仍开放：用户/DBA 回传 3.5.14-23a56ec / AuditLoader 5.0.0、固定列存在、一小时 237398/ERR3/按16000超长135、旧粗筛117、单次延迟约18秒。来源集群/账号安全引用、时区/截断配置、实际 SQL/候选字节限额、脱敏、多语句拆行和隔离测试环境仍待绑定。3.5.14 最终 LIKE/REGEXP 合成字面量验证、真实候选容量及两条查询成本是验收门槛，证据记在[公司侧清单](docs/starrocks-security-company-checklist.md)，不作运行时标记；4.1.4/Python/SQLite 不替代。

[PR #128](https://github.com/shixian66/xiaowei-agent-sdk/pull/128) 已合入 main@96c965c；以下候选验证/发行记录不等于公司验收或部署授权。CI 结果以对应 head 的检查为准，本次不打包；旧 3988aef 包不能用于验收，后续获准合并后须按最终 SHA 重新打包。本次离线验证：新增配置用例先复现 2 failed，修改后 runtime 安全巡检 3 passed、既有审计相关 10 passed；指定 Skill、S1–S5、定时 Agent/任务、分析/采集及文档回归 276 passed（`/tmp/xiaowei-security-scope-regression.log`），额外文档核对 2 passed，ruff 全仓、mypy 39 源文件及 diff 检查通过。这些是分组离线结果，不作完整套件或真实模型证明；下一项为推送两个独立提交并等待 CI，由用户复审新增差异，之后按公司清单完成真实验证；本地代码审查通过不豁免这些门槛。A0/A1b 真实验证与 A3 由用户/DBA 在公司执行，本地不连公司 StarRocks 或模型 API。已选 A，仅在公司隔离测试环境生成无害样本，生产不回放；真实模型、渠道实收、部署和用户验收未开始。未授权部署。

### 当前：S1 已复审通过，准备 PR 集成

S2 独立复审结论保留，审查版本 44b0d5d、受测代码 d750a41，具体证据见 §5。用户已确认 v4 回退限制并授权本地 S1，依据[计划 v1.5](docs/superpowers/plans/2026-10-22-feishu-input-presentation-repair.md)实现默认业务回答、单技术区与版本兼容，模型 schema 不变。D 及原目录工作保持不动。

下一项：按用户授权推送主题分支、创建 PR，确认远端 CI 全绿及 head 未变后合并；PR 记录精确 base/head、审查版本和集成证据。P1/P2 与 S2 均已独立复审通过，不重做整轮审查。已审查版本 ce52831 的源码/测试冻结于 b08c042；与最新 main 的唯一测试冲突保留主线实际请求容量校准，生产限额和前后 I/O 断言不变。c44bbdf 是集成前完整全套，不冒充集成后证据。真实模型、平台、公司源及部署不在当前授权内。v4 升级/前向修复的唯一权威契约见 [ARCHITECTURE](ARCHITECTURE.md#user-delivery-and-history)。

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

### 主线旧回答结构与历史证据（版本 ≤3）

以下记录版本 ≤3 的已合入行为，不覆盖本地 v4；当前差异和验证见 §5 S1，产品契约见长期交付规则。已合入的回答结构见[自由回答结构计划](docs/superpowers/plans/2026-10-17-free-answer-structure.md)。其中 S2 的“留 S3”等阶段文字是当时记录，当前合入状态以上表为准。

已合入的[模型自主 HTML/MD 报告](docs/superpowers/plans/2026-10-19-model-authored-reports.md)复用 blocks 和现有治理/交付；MD 原生排版、独立 HTML 片段与短 Skill，事实就近由代码填入，主消息提取文字。版本 3 贯穿保存/读取，解决旧纯文本在升级时未绑定字节的兼容问题；旧 HTML 基线字节回归保持一致。禁止脚本/外部资源，解析 CSS 值控制布局，文件/试排/预览只清理一次。A1 仍保留通用模型失败回执，未扩大数据/权限或 SQL 取数限额。

普通消息保留自由块顺序；本次 P1 复用 full/brief，本轮 brief 收起且完整交付，历史 brief 保持计数摘要及不自动导出原值的边界。remaining 与普通 metadata 默认收起；完整详情超卡片容量时沿既有分项附件交付。新 HTML 用静态详情，新 MD 分正文与证据明细文件，用绑定正文索引和文件内标题标明用途；旧绑定字节、键、uuid 不重排。未新增模型 schema、回答/数据库版本、持久化展示标志或展示服务；Web 保留 textContent。动作与审批材料沿完整专用契约。实现与离线验证不代表真实模型或飞书客户端达标。

Vertex 诊断已随 #117 合入。其候选历史记录为合成响应与应用测试 **344 passed**，ruff、mypy（34 文件）通过；真实 429 样本、公司环境和部署仍未验证，本轮未重跑该套件。不解析日志充当进度回调；新增观察只使用 SDK 与实际应用事件。

S3 的源码事实：`AgentAnswer` 为有序 `TextBlock | FactBlock`，另含 `report_format` 与 `awaiting_user`；模型文字累计上限为 16,000 UTF-8 字节、最多 64 块。归一化、输出预检、Session 提交与交付复用同一规则；本轮新事实的 brief 收起但完整交付，历史仍按可信原执行轮次生成摘要，重复引用取 full，不新增模型调用。符合补全类别的本轮结果进入“其余结果”，历史结果与未引用的诊断/发现原值不补全。动作区域置顶且不进附件；暂停材料显式引用本次请求执行的动作及本轮动作状态，不自动补入其他未请求执行的提案，暂停前已取得的需交付只读结果仍由交付补全。

`repair_final_output` 已通过 Runner 的 `invalid_final_output` 接入一次无工具改写；失败时不重放工具，被拒的原回答不进入后续历史。旧回答投影不套新输出预算，已绑定报告与分项重发复用原字节。方案 1 已有正式群入口的离线恢复证据：旧历史/暂停材料的批准、拒绝、重复/并发批准、发起人失效；无需据旧阶段描述重新实现恢复方案。

### 下一项工作与开放条件

B 的当前离线单元已交付；真实 U5 的日期解释、工具选择、答案质量、调用/耗时/用量及测试机器人接受/群内表现仍须具体授权后验证。保持默认关闭，不自动部署，不把离线候选当作真实验收；D 未实现。C 的独立受治理 Agent 任务已形成离线候选，版本与未覆盖项见 §5；以下为并存的旧业务实战缺口，不作为 B/C 的真实验收证据。

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

### S1/v4 P2(a) 附件回答优先级（用户确认独立复审通过）

本次起点 d29c2f0d7cff3365ffe9da02f97b97808999a7a0；Claude 对 ac93a74..d29c2f0（源码/测试 af797c09ef140c3ec5129b15dd3415be57c3e426）确认 P1 任务时间可见和 P2(b) 业务文件通过，但指出 P2(a) 新路径先给完整技术区留位、再截模型文字，违背回答优先。这一反例收窄了此前内部“P1/P2 无新增阻断”的结论。本次只修该根因，不重做 S1 或扩大范围。

feishu_render.build_feishu_message 的 v4 附件分支先试完整回答、必需说明和完整技术区，复用既有整行搜索安排业务预览；不能共存时整体去掉技术区，显示“技术详情超出消息容量，未附。”，再走既有回答优先路径。短技术区仍保留一个默认收起的面板；普通 v4、v2/v3/Legacy、已绑定内容原字节、权限/TTL/审批/unknown、模型与数据库版本不变。ARCHITECTURE 的长期规则已覆盖该优先级，无需新增契约。

新增 test_v4_attachment_keeps_complete_answer_before_technical_details 经真实 _data_parts 自动附件路径，400 行事实与先通过 AgentAnswer 校验的 15,915 UTF-8 字节模型文字：短 SQL 保留面板，5,000 个带引号字符/13,000 个普通字符的长 SQL 整区省略，完整回答、附件索引与事实限制均可见。审查示例约 8,000 汉字超过当前 16,000 字节模型文字上限，因此使用预算内样本复现，不放宽限额。

test_v4_attachment_uses_real_layout_when_empty_placeholders_are_larger 保留零行占位比完整行更大的边界，改为 22 条短事实、400 行大事实和 8,000 字节完整文字；旧样本的 16,000 字节文字原先被截断，不能再以保技术区为通过条件。新样本同时断言完整回答与面板可共存，并以独立变异确认遗漏下界搜索会失败，未删除该保护。

同根因的无技术区回退也保留完整回答试放：test_v4_attachment_without_technical_details_keeps_answer_with_many_short_facts 使用 34 条普通单行事实、短 SQL 和合法 8,000 字节回答，断言正文全文与末行可见、面板为 0 且明确未附。实际完整布局仅 17,020 字节/138 个组件，零行占位却有 213 个组件，不能先按占位截文字。该分支由增量审查指出后先补 RED，再在同一附件分支修复；独立复核 b08c042 已确认关闭，无新增阻断。结论只覆盖本次实际差异，用户已确认 Claude 对 ce52831 的复审无问题，授权 PR 集成。

源码/测试冻结 SHA：b08c042ad739ba5038999c9743bee8cc872f4875。以下正式检查均记录开始/结束 SHA 相同、工作区 clean、EXIT_CODE=0、VERSION_UNCHANGED=True；后续仅同步文档。日志目录 /tmp/xiaowei-s1-attachment-priority/：

| 检查 | 证据与实际结果 |
| --- | --- |
| 目标 RED | red.log：2 failed、1 passed，两种长 SQL 均在“完整回答可见”断言失败，短 SQL 成功；many-facts-red.log：1 failed，命中多事实回退丢失回答（先用 32 条事实加长 SQL 复现，最终采用 34 条短事实的更小反例）。最终通过证据见冻结回归 |
| 冻结版本相关回归 | related-b08c042.log：199 passed，29.99 秒；包含渲染、附件/报告、分项恢复、inline、v4/旧记录交付；命令全文在日志 |
| 技术区优先回退变异 | mutations-b08c042/technical-first-*.log：基线 3 passed → 变异 2 failed/1 passed → 恢复 3 passed；两例均因回答被截断而失败 |
| 零行占位提前拒收变异 | mutations-b08c042/zero-placeholder-rejection-*.log：基线 1 passed → 变异 1 failed → 恢复 1 passed；完整回答可保留的技术区被错删，命中面板数断言 |
| 无技术区完整回答回退变异 | mutations-b08c042/no-technical-full-answer-*.log：基线 1 passed → 变异 1 failed → 恢复 1 passed；撤掉第二次完整回答试放即命中全文丢失断言 |
| 静态与文档 | static-b08c042.log：全仓 Ruff、2 个修改 Python 文件 format、mypy 38 源文件、文档检查 2 passed、d29c2f0..HEAD 的 diff 检查通过 |

三组变异均在临时副本中运行；加载路径、源码 hash、退出码在日志，恢复后 hash 与冻结工作树一致，副本已删除。旧 af797c0 的 512 项相关回归、37 项浏览器及六组变异保留在 /tmp/xiaowei-s1-review-fixes/，仅证明该旧版本，不用来替代本次新增边界。P1/P2(b) 未改，按 Claude 已通过结论保留。

完整默认全套最后一次仍仅覆盖 c44bbdfce6a111784e435716820a71c670991d20：4873 passed、235 skipped、93 deselected、5 warnings，日志 /tmp/xiaowei-presentation-s1/full-c44bbdf.log。本次遵用户指令只跑受影响检查，不重跑或拼接称作全套通过。

未覆盖：真实模型的取证后澄清/SQL 草稿与补引用改写的自然度、调用/耗时/用量；拒绝审批恢复后的条件提示理解；真实 post/mentions/md、识图、桌面/手机折叠与飞书/代理实收。离线测试不证明真实任务能力，现有真实验收门槛保留。未推送/PR/合并/部署，未调用真实服务或读取公司配置凭据，D 保持暂停。

PR 集成以 main@96c965c 为基线，保留 #128；仅 handoff 与容量测试发生文本冲突，容量测试保留主线按真实请求校准的版本，不回退到硬编码 40KB。54 项交集检查覆盖首请求/工具后容量拒绝、数据库 Skill、正式手动巡检及自动工具拒绝、v4 展示和文档；lint、format、mypy（39 源文件）通过。精确集成 head、远端 CI 与合并结果写入 PR，不把旧候选全套当作集成后证据。PR #129 首轮 CI 39892841592（head 0aad449）8/9 通过，完整集成为 5008 passed、1 failed、1 teardown error、235 skipped、96 deselected：同一安全巡检参数 cite_fact=False 的脚本仍依赖 v3 自动补全，v4 无工具补引用多出一次模型调用导致耗尽。已在隔离 PG 复现同一 RED；仅给该参数补一次无工具引用回复，保留全部事实/脱敏断言，并新增准确模型次数与采集仅一次的断言。安全巡检/v4/Skill/正式入口/自动范围共 54 项局部回归通过（security-v4-green.log），生产代码、权限和 v4 规则未改；修复后远端完整 CI 仍须全绿。

### 群输入 S0/S2（v1.4，局部修复独立复审通过）

Claude 对 44b0d5d57ff94dc0d425d0df4c94715a8302fc55 的独立复审结论由用户提供：**通过，无仍须修复的问题**。受测代码为 d750a41bfb5466d70c7092b9c2346b2436e9fe40，其后至审查版本只改文档，395 项回归证据适用于该版本。本轮只修文档提示前缀并记录结论，不新增业务验证。已通过的修复位于 feishu_post.parse_post 的文档选择：仅一个可选文档时接纳，多 locale 或外层正文与 locale 并存整条按 invalid 拒绝；单文档 content_v2 优先且不回退旧值。invalid 表示无法确定唯一完整输入，不新增提示种类。模型 schema、保存版本、S1 展示与既有图片/权限边界未变。

拒绝测试改用已开启 SessionImageStore 和 Vertex 图片开关的 image_group 装配，逐项精确断言固定说明：多图→images，非法键→image_key；md 图片/files/media/未知节点/md HTML→unsupported；非法段落/非法 v2/多个文档→invalid。每项都断言零下载、零模型、零请求。纯解析层另有四项反例；旧多语言丢图“成功”夹具已换成单 locale 成功对照。当前纯解析用例 17 项。

RED：review-fix-red.log 中 5 failed、11 passed，失败分别为两种多文档各在解析/正式入口失守，以及两条错误 post 仅获一条说明；不是环境失败。最小修复后的局部回归 review-fix-green.log 为 57 passed。

两组变异分别在临时源码副本完成，先验证四项基线通过并记录实际加载的模块路径/hash：多图拒绝改为 continue，解析和正式入口共 2 failed；精确恢复后 2 passed。删除键正则同样 2 failed→恢复后 2 passed。日志为 `/tmp/xiaowei-post-s2/review-fix-mutant-multiple-images.log`、`review-fix-restored-multiple-images.log`、`review-fix-mutant-image-key.log`、`review-fix-restored-image-key.log`；临时副本已删除、工作树源码未被变异；基线及两组恢复后的 SOURCE_SHA256 均与冻结代码 d750a41 的 feishu_post.py 一致。初次副本缺 scripts 导致的装配错误单存 review-fix-mutation-setup-error.log，补齐已存在依赖后重跑，不把装配错误算作变异命中。

Claude 复审确认两种多文档在 _parse 阶段整条拒绝、零下载/模型/请求，11 项入口测试精确说明且不受容量/存储提前失败误导；本地两组变异因目标行为被破坏而命中，恢复源码 hash 与审查版本一致。审查方报告追加六组变异均被测试发现：多文档改取首个、多图只取首张、去键正则、post 错误回执去成员核对、去按消息去重、改回十分钟窗口。本轮仅记录审查方结论，未重跑这些变异。

可选项 1 已做：仅 post 的 input_notice 先核当前成员，再沿 _first 按消息去重，避免十分钟窗口吞掉下一条修正失败的说明。新增连续错误/重复投递/撤权反例；真正未支持类型与身份提示仍用原窗口。复审接受两条非阻断记录，不再调整：①同一错误 post 重复投递，每次先查成员目录再去重，与既有图片/文件顺序一致；②只 @ 机器人的 post 仍提示 invalid，与 text 的 EMPTY_COMMAND 不同，保留空输入准入及空命令路径。

最终代码冻结于 `d750a41bfb5466d70c7092b9c2346b2436e9fe40`。同次相关回归 **395 passed，83.95 秒，EXIT_CODE=0**，BEGIN/END_SHA 相同且 BEGIN/END_CLEAN、VERSION_UNCHANGED 均为 true；日志 `/tmp/xiaowei-post-s2/review-fix-regression-d750a41.log` 记录完整命令。覆盖用户指定的 6 个模块，加身份提示/真正未支持类型/群身份及原生批准恢复对照；不是完整默认全套。Ruff check、5 个相关 Python 文件 format、mypy（38 源文件）、文档命令检查（2 passed）、提交范围 diff 检查均通过。冻结后仅更新验证/复审记录和计划提示文案，源码和测试未变。本次不重跑完整默认全套；旧 4818 passed、235 skipped、89 deselected 的单次全套只覆盖 9f109374e57b89548b0aed2cd67e0a07f9819282（日志 full-9f10937.log），不能当成本次修复通过证据。早期 706/37 项记录及资源键隔离变异同样不替代本次检查，原始日志仍在 /tmp/xiaowei-post-s2/。

以上 S2 受测版本中，S0 的 v4 贯穿/回退方案仅为方案；原三项 Gateway→SDK→隔离 PG 暂停可见性结论不变：未恢复/过期后普通轮不可见，拒绝经恢复提交后可见，零重查/生产写。未实施 S1/v4/D，未新增依赖/迁移，未读取公司配置凭据或调用真实服务。未覆盖真实平台 post/mentions/md 形态：md 中 HTML 形式的 `<at>` 会整条拒绝，外层其他字典字段也会按多文档拒绝；两者偏安全但可能阻断合法使用，须在另获授权的真实验收中确认。真实识图、客户端展示和真实模型能力未验收。截至该 S2 局部交付时，完整默认全套最后一次仅针对 9f10937；395 项相关回归与变异不能合称全套。后续 S1 的独立全套见本节 S1 记录。v4 回退限制及 S1 本地开发已获后续授权，当前状态见 §1/§3；这些 S2 证据不证明 S1。

### 群输入与按需详情计划 v1.3（仅文档）

v1.2 冻结版本 c37631f5bc0ec76b3cb01198d7fe7f92fab3314c 已获独立文档复审通过；结论只覆盖该版本。复审新增 R1 经本轮静态核实成立：ResultDelivery 从保存版本重建回答并调用当前 validate_answer；旧 v3 的空 blocks 或部分引用依赖默认补全。必须按可信保存版本保留旧语义，与 Provider schema 是否变化无关；TurnAnswer 严格解析只接受 2/3，RequestRecord 类型声明也仅覆盖 2/3，旧程序会拒绝新版本。原生报告也有版本 3 消费点，已纳入方案/回归要求。仅改计划与 handoff，ARCHITECTURE 及源码/测试/依赖不变。

另核实 _paused 会 discard_pending，恢复上下文中的 Evidence 不等于普通 Session 已回放；计划要求实测未恢复/拒绝/过期后的索要路径，不可见时用可信材料说明尚未交付及条件，不承诺自动补发。仅监控失败的空回答、步骤用尽后的空收尾，需在实现时记录新增改写的实际调用与总预算约束；按版本的重发/历史矩阵已列入未来 RED，本轮未实施。

v1.2 准备阶段的 5 项零网络 post 合成诊断与 39 项旧渲染记录仍仅证明原基线，本轮未重跑或把其计作新功能证据。R1 本轮为源码静态核实，尚无新版本实现、隔离 PG 重发测试或真实模型证据。

文档检查：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/security/test_docs_command_consistency.py` 为 **2 passed**，`git diff --check` 通过；56 个本地链接/锚点有效，仅计划与 handoff 有差异，ARCHITECTURE/源码/测试/依赖未改。没有运行新功能测试、完整全套或真实模型/飞书/代理/公司源；未读取公司配置凭据。新 SHA 以本地交付 Git 输出为准，不为自写 SHA 再追加提交。

### 自建代理本地交付（已合入）

基线 be8c155638c4fa0c4eece912f8ff79fbef1c41ef；分支 codex/feishu-proxy-webhook。功能/测试冻结 SHA **163d2acb9ce4b1154d7cb3f995261c4075982c37**，文件树 **3b6b848ed689e172e9dac8c79a956678b8d0fbb3**；后续提交只补文档，不以新 SHA 冒充重跑全套。前候选 4f43d95 的独立审查发现 B/C get_task 未明确代理 HTTP 成功的界限，已补两类正式入口 RED，读取持久化 notification_reason 后提示“群内投递未确认”，重载为官方协议并更新目标版本后仍按历史回执说明。

产品改动为 webhook.py 的机器人协议配置/固定字段校验、保密 HTTP 请求格式/代理结果原因及回执，runtime.py 的安全配置诊断措辞；独立审查要求补齐 scheduled_tasks.py 的任务状态回执说明，仅从实际 attempt.reason 映射提示，不改调度/权限或输出 schema。现有本地 Tool、claim/settle、主体核验、接收范围、unknown 门槛和调度不变，不新增依赖/迁移。代理字段增加后发现固定超限说明也可能被固定 title/openUrl 撑过20000字节，先补正式 C 入口失败用例，再在 send_fixed 的最终请求处拒绝；正常容量说明仍使用本次唯一机会。

协议来源：用户提供 POST、JSON字符集、groupId 查询路由、title/message/openUrl 字符串及未知响应协议；用户最新决定不追加 at 标签。HTTP(S) 可信配置/唯一 groupId 校验、默认官方向后兼容和代理 2xx 的本地 reason 是落实既有接收与状态边界的实现选择，不是对真实代理行为的验证。不带 at 的实际兼容性、业务响应、飞书实收、真实模型 U5/U6/U7 及成本、30任务并发仍未覆盖。


当前离线证据：代理专项 **36 passed**（/tmp/xiaowei-proxy-task-status-green.log）；含最小闭环、B/C 回执新旧协议归因。无 at/open_url 的 RED **8 failed/14 passed**（/tmp/xiaowei-proxy-no-at-red.log）；固定说明仍超限 RED **1 failed**（/tmp/xiaowei-proxy-capacity-red.log）；B/C 状态 RED **2 failed**（/tmp/xiaowei-proxy-task-status-red.log），均在对应修复前命中目标断言。初次相关回归 **443 passed/1 failed** 为切换虚拟环境期间找不到证书文件，独立环境定点重跑 **1 passed**（/tmp/xiaowei-proxy-runtime-env-recheck.log）；不将分批相加为全套。

前候选 4f43d95 的全套在 /private/tmp 目录出现四项 Docker bind 源不存在的环境失败，为节省重复成本中止，**4 failed/1339 passed/1 skipped/89 deselected，EXIT_CODE=2，版本始终 clean/未变**（/tmp/xiaowei-proxy-full-4f43d95.log）；不是完整通过。已移动同一分支至上表 /Users 工作树，用离线缓存重建本项目独立 .venv，原失败的正式 Compose 入口 **1 passed/64.62秒**（/tmp/xiaowei-proxy-compose-after-move.log）。冻结 163d2ac 的完整默认全套结果如下。

ruff check、mypy（37源文件）、4个改动 Python 文件 format 和 diff 检查通过。扩大到 src/tests/scripts 的 format 检查报告4个基线已存在文件（cli.py、test_dialogue_real_model.py、test_monitoring_reliability.py、test_progress_sdk_contract.py），与 be8c155 无差异，本轮不混入格式整理；不宣称全仓 format 通过。

**最终有效证据（同一冻结版本，不拼接）：**

| 检查 | 命令/证据及结果 |
| --- | --- |
| 完整默认全套 | `PYTHONDONTWRITEBYTECODE=1 SDK_TEST_POSTGRES_URL=<本机隔离PG> .venv/bin/python /tmp/xiaowei_run_versioned_check.py /tmp/xiaowei-proxy-full-163d2ac.log .venv/bin/python -m pytest -q -p no:cacheprovider`；**4780 passed、235 skipped、89 deselected、5 warnings，1769.24秒**。日志起止 HEAD 均为163d2acb9ce4b1154d7cb3f995261c4075982c37，起止clean，EXIT_CODE=0、VERSION_UNCHANGED=True；覆盖受影响 A/B/C、Action/渠道、Evidence/Session、配置、迁移与部署维护。默认未选的浏览器及需显式环境的跳过项不算验收；5项warning来自网络拒绝测试的预期socket拦截 |
| 代理专项 | `.venv/bin/python -m pytest tests/sdk_core/test_webhook_proxy.py -q -p no:cacheprovider`（同上本机PG及禁字节码环境）；**36 passed**。正式群/SDK/治理/隔离PG保持，模型/HTTP/飞书/业务驱动为合成替身；不能证明真实自主选择或公司接口 |
| 静态/文档 | `.venv/bin/ruff check --no-cache .`、`.venv/bin/mypy --cache-dir=/tmp/xiaowei-proxy-mypy src`（37文件）、`.venv/bin/ruff format --check src/xiaowei/webhook.py src/xiaowei/runtime.py src/xiaowei/scheduled_tasks.py tests/sdk_core/test_webhook_proxy.py`、`git diff --check`通过；受影响文档相对文件链接存在；默认全套包含文档命令检查 |
| 隔离变异 | /tmp/xiaowei_proxy_mutations.py 以163d2ac独立复制src，覆盖 mention 屏蔽、代理2xx响应分支、固定说明容量门槛、持久化回执原因；**4/4被杀死**，各命中目标断言、非setup错误。日志 /tmp/xiaowei-proxy-mutation-{mention,response,capacity,receipt}.log 含基线SHA、源码副本及文件摘要；未改真实工作树 |
| 独立审查 | be8c155..4f43d95发现任务状态回执P2；增量4f43d95..163d2ac只读复审关闭P2、无新增阻断。检查实际调用路径与新测试/RED/GREEN日志，未重跑重型全套；不是本线程自行复查冒充独立审查 |

关键回归名称：`test_proxy_formal_entry_preserves_query_json_and_truthful_http_receipt`（含POST前已落库sending及秘密哨兵）、`test_proxy_same_intent_deduplicates_after_mention_sanitization`、`test_proxy_unknown_gate_requires_delivered_warning_before_explicit_resend`、`test_proxy_sender_recheck_prevents_claim_and_http`、`test_proxy_unapproved_receiver_has_zero_http`、`test_proxy_fixed_reminder_uses_same_once_only_path`、`test_proxy_scheduled_agent_delivers_facts_or_one_capacity_notice`、`test_proxy_static_fields_cannot_make_capacity_notice_exceed_limit`、`test_proxy_task_status_explains_persisted_http_receipt_even_after_config_change`、`test_official_and_proxy_bots_keep_distinct_wire_contracts`。

仍未覆盖：真实代理对“不带at”的兼容、业务响应协议及真实飞书群实收；真实模型U5/U6/U7与成本、30任务并发实测。上述本地交付记录产生时尚无推送/PR/远端CI/合并/部署证据，也没有真实服务调用或公司配置/凭据读取；当前新增的 GitHub 集成授权见 §1/§3，D保持暂停。代理不新增迁移或依赖，C主线v15的停服/备份升级边界仍按README，不因本次通过而取得部署授权。

### C 收尾修复与验证

功能冻结版本 `69820b1e2809139117494294e5e0a15eb6b3b73f`，本轮比较基线 `c7b1412eb27669a6da19d89123fb398a864e66e4`；分支 `codex/scheduled-agent-tasks`。本轮仅修复 F1/F2 的用户提示，不改变权限、Evidence、审批、核验次数或调度契约。C 此前精简、B1/B2 关闭及删减/变异记录见 `git show c7b1412:AGENT_HANDOFF.md`；本轮独立复审只覆盖这两处差异，不能解释为重新审查全部 C。

| 根因 | 修复与行为证据 |
| --- | --- |
| F1 后台静态授权撤销与定义失效共用异常，被误报为 definition_unavailable | TaskDefinitions.validate 用窄义 AuthorizationRevokedError 区分当前通知/工具/目标授权缺失；_current_definition 映射 authorization_revoked。不增加核验调用。`test_current_config_revocation_stops_before_model_and_business_post` 覆盖通知、查询工具、目标及 automatic_tools 撤销，断言具体停止原因/原群说明、零模型调用及零业务 POST；目标版本变化仍 recipient_changed。`test_definition_dependencies_obey_current_model_policy_before_background_run` 保留定义失效的 definition_unavailable 反例及允许对照 |
| F2 控制更新未改变任何行时，未发送的 B 提醒也被覆盖为“发送已准入” | control_task 仅在 receipt_id 非空时显示发送已准入；已停止 Agent 仍显示任务已停止。新增 `test_cancelled_fixed_reminder_repeat_cancel_does_not_claim_send_admission`：正式群入口批准后连续取消两次，保持 cancelled，回复无发送准入、无 attempt/POST，到点也不发送；原 `test_completed_fixed_reminder_keeps_admitted_control_reason` 已发送对照保留 |

**B 兼容说明：** c7b1412 已有的 send_fixed 类型结果也用于 B 固定提醒。发送前主体核验暂时失败（包括成员接口异常）返回 not_sent/sender_not_authorized，由提醒执行路径记 authorization_unverified、unverified_count 加 1，零 POST；不再抛普通异常。尚未占用发送机会的 active 提醒可在迟到窗口内再次核验；已占用或 unknown 的机会绝不自动重发。本轮仅补录此事实，没有新增重试或授权范围。

| 检查 | 当前证据 |
| --- | --- |
| RED 先复现 | `/tmp/xiaowei-c-f1f2-red.log`：4 failed、4 passed，复现三类静态撤销原因及重复取消误报；产品源码当时仍为 c7b1412。该记录含测试工作区变更，不作冻结 SHA 的全套证据 |
| C/B 相关回归 | **133 passed，115.88秒**，`/tmp/xiaowei-c-f1f2-green.log`；两个 scheduled 测试文件，正式入口、真 SDK、脚本模型、隔离 PG/传输替身。提交前局部检查，不拼作全套 |
| 冻结版本单次完整默认全套 | **4744 passed, 235 skipped, 89 deselected, 5 warnings in 1732.13s (0:28:52)**；`/tmp/xiaowei-c-f1f2-69820b1-full.log`。开始/结束均为 `69820b1e2809139117494294e5e0a15eb6b3b73f`，工作树 clean、EXIT_CODE=0、VERSION_UNCHANGED=True。包含受影响 C/B/A、Action/群审批、Evidence/Session、迁移及本地 Docker 部署维护回归；跳过和未选项不算验收 |
| 冻结版本静态 | `/tmp/xiaowei-c-f1f2-69820b1-static.log`：全仓 ruff、3 个受影响 Python 文件 format、mypy src（37 文件）、基线差异检查全部通过；同 SHA、前后 clean、EXIT_CODE=0 |
| F1/F2 独立复审 | `69820b1e2809139117494294e5e0a15eb6b3b73f` 相对 c7b1412 的两处修复通过、无阻断；复审者另跑 **9 passed，10.82秒**，覆盖五类当前配置撤销、定义策略允许/拒绝和 B 未发送/已发送控制对照。`/tmp/xiaowei-c-f1f2-independent-69820b1.log` 保存 pytest 输出；前后 SHA/clean 由复审工具会话核对，该文件没有版本头，与冻结全套日志分别记录 |

命令均在本工作树运行。pytest 使用 `SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres PYTHONDONTWRITEBYTECODE=1`，相关检查执行 `.venv/bin/python -m pytest -q -p no:cacheprovider tests/sdk_core/test_scheduled_agent.py tests/sdk_core/test_scheduled_tasks.py`；完整默认全套执行 `.venv/bin/python -m pytest -q -p no:cacheprovider`。冻结检查由 `/tmp/xiaowei_run_versioned_check.py LOG COMMAND...` 记录 SHA/树/clean/退出码。静态执行 `.venv/bin/ruff check --no-cache .`、`.venv/bin/ruff format --check src/xiaowei/scheduled_tasks.py tests/sdk_core/test_scheduled_agent.py tests/sdk_core/test_scheduled_tasks.py`、`.venv/bin/mypy --cache-dir=/tmp/xiaowei-c-f1f2-mypy src`、`git diff --check c7b1412..HEAD`。

全套结束后仅更新本交接；文档命令一致性检查 2 passed（0.14秒），git diff --check 通过，git diff --exit-code 69820b1 -- src tests pyproject.toml uv.lock 确认源码、测试和依赖相对冻结版本无差异，不为文档提交重复运行重型全套。本轮未新增隔离变异：差异仅为原因分类/反馈，RED 已分别击中原错误，原治理与并发保护未改。

用户已确认 Claude 对 `a6113eb57079e195341ef705ad20956ef376d967` 的 F1/F2 复审通过、无阻断，并授权推送、建 PR、远端 CI 全绿后合并；本次授权同步只修改交接，功能仍为上述冻结 SHA。远端 CI 和合入状态统一记录在 PR。剩余缺口：真实 U6/U7 的自主选择/调查/答案质量及模型耗时费用、真实飞书实收、30 任务实际并发/成本。D 未实现。继续默认关闭，暂不部署；真实环境和费用须另获授权。未调用真实模型/Webhook/飞书/公司源，未读取公司配置或凭据正文，原目录修改未混入。

### 合并交付前主线集成

B 已审查候选为 `815c00f7bff6212990748eb5abc370f8ee57c129`。交付前同步主线 PR #124（`de93ed183257d96a294376c166d6d18199b01020`）；源码自动合并，仅本交接快照冲突，保留 B 状态并更新授权。监控补查修复的有效说明、失败分类与真实验证缺口继续由 [监控评测说明](docs/monitoring-reliability-evaluation.md#监控补查修复候选2026-10-21) 和 [回退限制](deploy/OPERATIONS.md#监控补查修订的回退限制) 维护；不重复保留其已过期的“待用户授权”状态。合并后的精确SHA、检查结果和CI链接记录在本次PR；下列本地证据仍只覆盖注明SHA，不自动覆盖合并树。

### 2026-10-22 B：R1 连续使用修复

主线基线 `9284688bb8907c9968e84d5e49c23e551c53ac58`；本轮输入 `e055ca943e77030107ffdd37d802f27b9fb23d74`，产品修复 `6864bb758b76371ff1fb10acfc24f9a5291899fa`；`369435d9e961db5c669f85c44b688496a6719037` 仅补 README 扩权版本提醒，源码与测试不变，单次全套在369435d执行。后续只更新交接/计划状态。外部复审已确认 B1/C1/N1/N2/N3 关闭；对应实现和测试映射保存在 `git show e055ca9:AGENT_HANDOFF.md`，不能用其535项/4657项结果证明本轮 R1。

**R1 已确认：** 原 README 只给业务查询接收范围，B 起草/激活/状态 Evidence 进入共享历史后，下一次即时通知或提醒因缺 scheduled 接收许可被拒。原 C1 测试两次 /新建 掩盖了此问题，这是验证遗漏。修复采用管理员显式配置（README 的最小完整列表），不自动给 scheduled 授权，也不跳过全部模型可见证据、定义依赖或业务源检查。缺配时模型工具说明、起草拒绝及通知的代码事实提醒指出 receive_tools/receive_targets 和 scheduled 依赖；不扩大结果 schema 或修改历史证据指纹。

| 检查 | 当前证据 |
| --- | --- |
| TDD 工作区复现 | `/tmp/xiaowei-b-r1-red.log`：3 failed、1 passed，复现确认后即时通知被挡、第二次提醒被挡及缺配无指导；修改前未带版本头，不作为精确提交的正式QA记录 |
| 工作区最小闭环 | `/tmp/xiaowei-b-r1-green.log`：6 passed，8.22秒。公开README策略直接进入正式群处理链/真SDK/治理/隔离PG/HTTP替身；两条提醒各发送一次、即时通知、C1共享门槛全程不 /新建；原业务源直接/传递拒绝保留。此记录不替代冻结版本检查 |
| 冻结版本关联检查 | **476 passed，129.39秒**，`/tmp/xiaowei-b-r1-related.log`；B专项68项、通知、feishu_render、治理、飞书协议、Evidence范围及文档。HEAD=6864bb7，开始/结束均clean、版本不变。静态日志 `/tmp/xiaowei-b-r1-{lint,format,mypy,diff}.log` 同SHA，四项0退出，mypy37源文件 |
| 单次完整默认全套 | **4660 passed, 235 skipped, 89 deselected, 5 warnings in 1576.51s (0:26:16)**，`/tmp/xiaowei-b-r1-full.log`；HEAD=369435d，开始/结束SHA相同、工作树clean、EXIT_CODE=0、VERSION_UNCHANGED=True。无变异插件；旧无版本头日志不用于覆盖R1，跳过/未选中项不算能力验收 |
| 独立复审 | **6864bb7 本次差异复审通过，R1关闭**；独立8 passed，9.57秒（3新增、C1、传递False/True、2项TTL），差异检查通过、版本/工作区未变。唯一P3文档提醒已在369435d补齐：既有接收范围扩大须更新target_version并重新授权；非执行层变更 |

**隔离变异：** `/tmp/xiaowei-b-r1-mutation-transitive.log` 移除 `TaskDefinitions.check_content` 后，原 `source_allowed=False` 由零POST变为一次POST，目标1 failed；`/tmp/xiaowei-b-r1-mutation-implicit.log` 自动补六项scheduled接收范围及目标后，缺配回归目标1 failed。两种均仅进程内变异，HEAD=6864bb7、前后clean/版本不变；预期失败不计为正式通过。

正式检查使用本机 `/tmp/xiaowei_run_versioned_check.py`，开始记录 HEAD_SHA/TREE_SHA/WORKTREE_STATUS/COMMAND，结束记录 END_HEAD_SHA/END_WORKTREE_STATUS/EXIT_CODE/VERSION_UNCHANGED；要求干净工作树且版本不变，不输出环境变量全集或任何凭据。旧日志不会补写成当时已捕获SHA的新证据。

新增/加强用例：`test_documented_receiver_supports_consecutive_reminders_and_immediate_send`、`test_missing_task_receiver_permission_explains_configuration_without_http`、`test_task_receiver_permission_never_inherits_business_source_permission`；原 `test_approved_reminders_ignore_other_unknown_but_immediate_send_still_warns` 改为同 Session 且读取公开配置，删除两次 /新建，不削弱断言。原跨任务材料传递的 source_allowed=False/True 对照继续保留。

两项非阻断建议暂不扩展：过时批准的 Action/用户回执已确定拒绝、draft 不可再激活，不为 R1 改生命周期；旧v14自动探测不新增，沿 README 已接受的重建或恢复v13配对备份路径。应用仍schema14，本轮无迁移/依赖变更。

可复现命令（在本工作树；pytest前缀为 `SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres PYTHONDONTWRITEBYTECODE=1`）：

- 关联：`.venv/bin/python -m pytest -q -p no:cacheprovider tests/sdk_core/test_{scheduled_tasks,webhook_notification,feishu_render,governance,feishu,evidence_scope}.py tests/security/test_docs_command_consistency.py`。
- 单次完整默认全套：`.venv/bin/python -m pytest -q -p no:cacheprovider`。
- 静态：`.venv/bin/ruff check --no-cache .`；`.venv/bin/ruff format --check src/xiaowei/{feishu_render,tools,webhook}.py tests/sdk_core/test_scheduled_tasks.py`；`.venv/bin/mypy --cache-dir=/tmp/xiaowei-b-r1-mypy src`；`git diff --check e055ca9..HEAD`。
- 这些命令由临时版本记录器包裹运行，实际展开的COMMAND保存在各日志；变异另加 `-p xiaowei_b_r1_mutations`，以 `XW_R1_MUTATION=transitive_scope/implicit_task_scope` 选择保护，插件路径 `/tmp/xiaowei_b_r1_mutations.py`。日志已写明MUTATION类别，不混入正式全套。

**边界与下一步：** B 外部复审已通过，用户已授权推送、建PR并在远端CI全绿后合并；尚未部署；不读取公司配置/凭据，不调用真实模型、Webhook、飞书或公司源。离线脚本模型不证明真实 Agent 能力。真实 U5（日期理解、澄清、工具选择、成本及平台闭环）未验收；本轮单次完整默认全套已执行通过，精确结果见上表。C/D 不实施，原目录修改保留。

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
- OpenAI 协议族的 SDK 内置 `conversation_locked` 重试不经过应用的 429/503 策略；Vertex 不触发。选择器组合失败后的子集重试不做语义推导；均为已有记录的边界。
- C8 数据源写前摘要比对不等于原子版本控制，真实环境开放前须按架构核对竞争窗口；Annotation 回读 `limit=100` 边缘情况未关闭。未知插件普通字段中无法识别的凭据风险与写端令牌范围按既有取舍处理，不增加插件白名单。
- 单个大字段超限时预览仍可能没有条目；监控工具返回格式与客户端显示缺口不能用模型编造数据补齐。图形 D4 暂缓，保留已完成的协议核验供明确重启时使用。
- 历史 secret-scan 随机样本不稳定、Grafana 群重发整文件失败及 C8 取消落定偶发失败有记录；单次重跑通过不能核销原因，旧失败记录也不证明当前版本必然复现。
- 显式开启 tracing 的真实字段/接收边界、真实模型任务质量与公司端到端验收仍需对应证据；默认关闭 tracing 的离线检查不能替代这些验证。
