# 小维：当前交接

> 核对日期：2026-10-29，Asia/Shanghai。本文只维护当前代码／计划状态、验证证据、缺口和下一项工作。长期契约见 [ARCHITECTURE.md](ARCHITECTURE.md)，阶段与退出条件见 [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)，用法见 [README.md](README.md)／[OPERATIONS.md](deploy/OPERATIONS.md)。

**OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**

## 1. 当前快照

| 项目 | 本次核对事实 |
| --- | --- |
| 仓库／产品基线 | [shixian66/xiaowei-agent-sdk](https://github.com/shixian66/xiaowei-agent-sdk)，fetch 后 `origin/main@7565c6a73e863fd791878990d0fd23bc3f33560e`，包含 DS PR #145 |
| 当前任务 | 只修订四份根文档，使范围、实现状态、用法和路线与当前主线一致；分支 `codex/docs-architecture-alignment` 从上述主线建立，不改变产品或权限契约 |
| 隔离与已有工作 | 复用干净隔离工作树；桌面旧 checkout `b89d4ee` 及其三份文档／其他任务未提交材料保持原样，不把旧 checkout 当最新实现 |
| 源码／入口 | 唯一产品包 `src/xiaowei`；`xiaowei` 与 `python -m xiaowei` 为正式入口。旧 Runtime、Worker、专属测试／迁移入口已移除，兼容旧回答和已绑定交付的路径仍保留 |
| 存储与回答 | 应用 schema **17**：016 保存有界定时结果快照，017 只新增 `ds_clusters`；新回答为可信 v4，旧 v2/v3/Legacy 和已绑定原字节按原语义读取。SDK 表不改 |
| 锁定依赖 | Python 3.11；Agents SDK 0.22.3、OpenAI 3.20.0、MCP 2.2.0、Lark Channel 1.4.0、sqlglot 30.17.0、httpx2 2.13.1、nh3 0.3.7、tinycss2 1.5.1、Pillow 13.0.0；以 uv.lock 为准。CI 维护已锁 certifi 2026.10.19、开发链 filelock 4.2.1／msgpack 1.2.3 |
| 最近完整主线集成证据 | [PR #145](https://github.com/shixian66/xiaowei-agent-sdk/pull/145) 的精确受测树与上述 squash 合并树相同；[CI #421](https://github.com/shixian66/xiaowei-agent-sdk/actions/runs/40721039641) 9/9 成功，详情见 §5 |
| 已记录发行／公司部署 | 2026-10-28 运维交接记录：用户操作部署 `916f57a26d2bac201b6772a48016f194af7d586e`，config valid、应用／PostgreSQL healthy、schema16；对应[发行运行](https://github.com/shixian66/xiaowei-agent-sdk/actions/runs/40653486879)。这是历史用户回报，本轮未核验服务器现状；不能推定 DS/schema17 已部署或业务已验收 |
| 本轮授权边界 | 文档核对、修正和必要离线检查；不写功能代码、不改公司配置、不调用真实模型或公司服务、不部署。DS 的既有推送／条件合并授权已完成，不作为本次文档分支的推送／合并授权 |

接手仍先核对实际 HEAD、工作区和用户新指令。下列“已合入”只描述 Git 事实；独立审查、离线测试、隔离真实软件、公司部署、渠道实收和用户接受分别记证。

## 2. 当前能力与契约索引

| 能力 | 主线实现与权威契约 | 状态边界 |
| --- | --- | --- |
| 数据库查询／诊断 | StarRocks 与 TiDB 本地 Adapter，多目标、真实账号可读范围、SQLGuard、保真 SQL、结构／计划／审计或诊断表；[§6](ARCHITECTURE.md#6-只读查询保护) | 两种方言、默认库、EXPLAIN 与失败规则不同；不互相套用 |
| 环境选择 | 数据库沿 [R7](ARCHITECTURE.md#r7-targets) 的对话／Skill；DS 另有[可信轮内集合](ARCHITECTURE.md#dolphinscheduler-environment) | 数据库没有 DS 式硬选择校验；DS 自然语言解析仍须真实模型验收 |
| 监控调查／选中写 | [三源及 Action 契约](ARCHITECTURE.md#monitoring-mcp)，SDK 原生暂停／恢复 | 只开放已登记读工具和逐次批准的写动作；密码私聊命令尚未实现 |
| 交付与输入 | [v4 按需详情／原生报告](ARCHITECTURE.md#user-delivery-and-history)、[群 post／单图](ARCHITECTURE.md#feishu-input-presentation-repair)、[进度](ARCHITECTURE.md#progress-observation) | 当前可运行；真实模型质量、图片及平台／客户端行为仍有开放项 |
| 通知与定时 | [A/B/C 与自建代理](ARCHITECTURE.md#scheduled-agent-tasks) | 默认关闭、创建与执行复核、unknown 不重放；Node D 暂停 |
| 运维历史 | [H1/H2、D1–D3](ARCHITECTURE.md#operations-history) | 新会话可受治理地追溯旧事实和结论；定时任务不读群交互历史，B1(a) 保持 |
| 安全巡检 | [v2 检测及成文规则](ARCHITECTURE.md#security-context-reuse-proposal) | 注入／延迟线索，多语句仅辅助；旧 v1 事实保持，不能用规则命中证明攻击成功 |
| CloudCanal | [接入设计](ARCHITECTURE.md#cloudcanal-mcp)和独立 CC0 探针 | 产品 MCP 工具尚未接入；需现场核约再决定 CC1 |

当前正式 ServeConfig 的 `targets` 至少包含一个数据库目标；DS／监控为可选源，不能仅凭内部无数据库 Agent 的测试宣称“纯 DS／纯监控部署”已支持。此处记录实际配置限制，不新增限制或改变长期方向。当前 tracing 始终显式关闭，也没有 ServeConfig 开启入口。

## 3. 当前计划与下一项工作

四份根文档已按上述产品基线完成对齐和下述文档检查，交付本地文档分支供审阅。下一项由用户选择业务工作或公司验收，不根据旧段落中的“下一步推送／开始开发”自动执行。新增功能、模型／数据授权、发行和部署各按当前明确指令办理。

### 主线合入索引

以下已核对 Git；#129、#135、#137、#140、#142、#143、#145 另核对 GitHub 的 merged 状态。历史 PR 正文中的“待合并”不能覆盖实际合并事实。

| 工作 | 合入版本 | 后续仍需验证 |
| --- | --- | --- |
| 群输入与 v4 业务结果交付 | [#129](https://github.com/shixian66/xiaowei-agent-sdk/pull/129)，`b7d033c` | 模型表达、post／mentions／图片、真实客户端与分项实收 |
| StarRocks 明确解析失败修正 | [#135](https://github.com/shixian66/xiaowei-agent-sdk/pull/135)，`da7f32e` | 公司 pre 版本／驱动签名、真实 Agent 的同义修正与诊断 |
| TiDB A / B/C | [#136](https://github.com/shixian66/xiaowei-agent-sdk/pull/136) `22f7446`；[#137](https://github.com/shixian66/xiaowei-agent-sdk/pull/137) `9d97d5f` | T1–T8 及三集群公司验收，B4 独立结论 |
| 历史 H1 / H2 | [#138](https://github.com/shixian66/xiaowei-agent-sdk/pull/138) `3d37ac7`；[#139](https://github.com/shixian66/xiaowei-agent-sdk/pull/139) `bac5df3` | H3 真实检索／背景复用与诊断质量 |
| 安全巡检 I1 成文 | [#140](https://github.com/shixian66/xiaowei-agent-sdk/pull/140)，`c1c28a2` | I3 的真实措辞／效果；按证据决定 I2 |
| 审批回执与定时状态归因 | [#142](https://github.com/shixian66/xiaowei-agent-sdk/pull/142)，`0b401b2` | 公司 C1–C4，含旧待批提案与实际批准／送达路径 |
| 安全巡检攻击线索收窄 | [#143](https://github.com/shixian66/xiaowei-agent-sdk/pull/143)，`916f57a` | 新候选覆盖／容量、A0/A3 与真实 Agent／渠道 |
| CI 审计与扫描自测维护 | [#144](https://github.com/shixian66/xiaowei-agent-sdk/pull/144)，`350345d` | 已修复当时 msgpack 审计及随机扫描诱饵问题；不再列为未解决缺陷 |
| DS API 只读 | [#145](https://github.com/shixian66/xiaowei-agent-sdk/pull/145)，`7565c6a` | 两版公司 API、D1–D4／19 变体、长日志负载和用户验收 |

A/B/C 通知与定时已分别随 #123 `9284688`、#125 `2bee11f`、#126 `be8c155` 合入；自建代理 #127 `1a92c58`。原生报告、自主调查、普通消息和进度已在主线，后续 #129 统一了 v4 交付，不能再从旧计划标题推定未实现。手动安全巡检 #128、权限简化 #130、展示 #132 和定时失败通知 #133 同样是当前实现的前序，不是待开发切片。

<a id="dolphinscheduler-implementation"></a>

### DS：离线交付已合入，实战验收开放

产品契约只见 [ARCHITECTURE](ARCHITECTURE.md#dolphinscheduler-environment)，工具、限制和固定 D1–D4／19 变体只见 [DS-RO-P1-r6](docs/superpowers/plans/2026-10-28-dolphinscheduler-readonly-api.md#ds-implementation-plan)。S1–S4 与审查修订、R1 日志业务 JSON 分类、C1 四项身份绑定、C2 删除多余选择字段均已合入；保留 `ds_read_scope` 原边界。schema17 只有 `ds_clusters`，不兼容从未发布的旧 DS 测试候选结构。

PR base `916f57a26d2bac201b6772a48016f194af7d586e`，head `bba19d4ba72c43524211f5afb523b70862ba68f7`。rebase 无冲突；独立 C1/C2 复审覆盖源码候选 `b469b5bcca51335fe40fa799346b886d9bae1061` 及集成路径，无代码阻断，另验 9 项和实际应用绑定 8 种变化。最终 head 只修文档，src/tests 不变；合并树与受测树一致，未部署。

用户明确本地只做离线开发，公司模型与 DS 隔离。下一步需由用户在公司按原样例验收：环境自然澄清／切换、失败定位、长日志中间数据库错误、子运行／等待／恢复追问、每源权限／时区／业务码、服务器大日志负载及脱敏延迟、飞书群实收。当前 CI 只能关闭相应离线项，不证明 Agent 自主诊断达标。

### TiDB、历史与安全巡检

- **TiDB A/B/C：** 已合入；精确 v6.5.5／v7.5.3 本地 TiUP、15 个诊断来源、结构／视图／计划／锁等待及正式 Web 真实库配脚本模型有证据。公司 3 集群、TiFlash、TLS／资源／权限、模型 T1–T8、真实渠道及用户接受仍按[公司清单](docs/superpowers/plans/2026-10-24-tidb-query-diagnosis.md#tidb-company-acceptance)逐项判断。不能再将 PR #137 写成未合并候选。
- **H1/H2：** 已合入；有界定时结果快照、失效清理、当前原源复核已落地。H3 的真实取证／因果解释未验收，见[计划](docs/superpowers/plans/2026-10-26-operations-history-and-knowledge.md)。独立分支的离线材料不能自动成为主线或真实评测结果。
- **I1 与检测 v2：** 已合入；成文遵循当前证据，操作类型检测已删除，OR／延迟保留、多语句辅助。I3 真实措辞和背景复用、I2 触发条件见[计划 §9](docs/superpowers/plans/2026-10-23-security-inspection-report-presentation.md#security-context-r3)。安全巡检公司验证沿[清单](docs/starrocks-security-company-checklist.md)；旧 v1 237398 条／117 候选的公司记录不证明新 v2 候选覆盖，需新代表小时。

<a id="sql-parse-recovery-status"></a>

### StarRocks 解析失败与监控补查

[#135](https://github.com/shixian66/xiaowei-agent-sdk/pull/135) 只恢复 Adapter 在业务 execute 阶段明确识别的解析失败，原始异常不外发；其他 SQL 执行错误、未知结果及固定模板按原边界。离线正式链已通过，公司 pre 实际版本、完整驱动形态和真实 Agent S1/S2 保持[原计划](docs/superpowers/plans/2026-10-24-starrocks-sql-parse-recovery.md)门槛。

<a id="当前独立候选监控补查修复"></a>

监控补查已随 [#124](https://github.com/shixian66/xiaowei-agent-sdk/pull/124) `de93ed1` 合入，不再是独立候选。选择器／对象缺失／有界响应修正沿当前监控契约；[11 场景、14 轮评测](docs/monitoring-reliability-evaluation.md)的合成导出只证明给定事实的解释路径，真实自主取证、补查质量和公司监控仍开放。

<a id="cloudcanal-phase1-status"></a>

### 尚未实现或暂停的方向

| 方向 | 当前状态／继续条件 |
| --- | --- |
| CloudCanal | #134 `b208b2c` 合入一期计划和独立 CC0 脚本，产品未接入；用户在 6.4.0.2 公司环境确认只读账号链接、工具／错误／分页／日志语义后复核，再获授权推进 CC1。用法见 [README](README.md#cloudcanal-cc0) |
| Node 核心巡检 D | 暂停、未实现；C 的通用 Agent 能调查不代表固定 Node 样例已验证，不自动重启 D |
| 监控曲线 D4 | 暂缓，协议核验和候选方案留在[交付计划](docs/superpowers/plans/2026-10-14-result-delivery-documents.md)，仅在明确重启时接续 |
| Grafana 密码私聊 | 已批准独立设计、未实现；当前仍由管理员在 Grafana UI 配置 |
| I2／Wiki／通用历史扩展 | I2 按 I3 证据决定；Wiki 仅调研；定时读取群交互历史的 B1(b) 未批准 |

## 4. 实测环境与兼容性缺口

已有证据只覆盖原 SHA、配置和范围；本轮核对文档／源码，不重跑真实服务，也不把历史用户反馈当作当前服务器状态。

| 对象 | 有效证据层 | 未关闭项 |
| --- | --- | --- |
| OpenAI Responses | 真 SDK＋HTTP 替身 | 具体获准端点／模型的真实工具闭环、结构化输出和追问 |
| Gemini 兼容端点 | 2026-09-30 真实 API 曾验证部分工具／签名／类型回答，受 503/429 影响未闭环 | 当前模型完整续轮、交付和签名回放；历史额度不能当当前事实 |
| DeepSeek／兼容网关 | DeepSeek 协议替身；历史第三方 V4 Flash 格式组合不兼容。`6389d78` 的 glm-4.7-flash Gate0 单次真实合成工具验证 | 不推广到官方端点或其他组合；稳定任务质量、业务工具与飞书未由此证明 |
| Vertex | 公开 Model 薄适配、多调用／签名／schema 离线证据；公司结果及 429／回答拒收日志证明曾调用 | I-V B/C、当前 schema／改写／退避质量、旧历史影响、12 图请求；已有调用不等于完整模型验收 |
| PostgreSQL／Session | 隔离 PG16.15，初始化、迁移至17、稳定密钥、SDK 暂停恢复／历史、备份与隔离恢复；最新 CI 发行84项 | 公司容量、备份保留、故障主机恢复演练；新 PG 镜像版本需另验 |
| StarRocks | 可丢弃 4.1.4 真协议、权限、LOGICAL 零执行、原文 SQL；#101 的43项仅该版本。公司有查询／误选反馈及3.5.14安全巡检记录 | 每目标版本、TLS／资源组、实际账号、时区／日期函数和固定任务成套验收；其他集群版本不能推定 pre 版本 |
| TiDB | 本地 TiUP 精确6.5.5／7.5.3、单PD/TiKV/TiDB、15诊断表、视图和计划、合成锁等待；真库＋脚本模型入口 | 公司三集群、TiFlash／监控、TLS／PROCESS／内存与资源行为、T1–T8真实模型；SLEEP返回1不证明数据库超时已中止 |
| DS | 两版协议夹具、10 MiB 中间错误／超长行／截断／超时、真实应用／SDK／PG／Chrome；DS 与模型为替身 | 公司API、业务码、任务类型／动态子流程、权限／时区、日志负载／脱敏延迟、真实 Agent D1–D4／19变体 |
| Prometheus MCP | 官方 v0.18.0 二进制连接合成本机 API，真 SDK 协议／治理 | 公司服务端超时／样本／并发、真实指标与自主调查质量 |
| Grafana MCP／Grafana | 官方 MCP2.0.2 与可丢弃 Grafana13.2.3／PG，D7/C8 实际写／回读及拒绝 | 公司组织／账号／插件／网络、写端权限及竞争窗口；CI跳过的真实二进制用例不计通过 |
| Alertmanager | APIv2 替身及早期0.34.1隔离记录，正式入口离线反馈 | 公司版本、认证／HA／容量、写后回读和群审批 |
| 飞书／通知／定时 | 官方SDK、协议替身、卡片/post、附件、分项恢复和公司使用反馈；A/B/C及代理离线链 | 真实 mentions／图片形态、create/reply/apatch权限、原消息回复、限频／uuid、多附件、桌面手机折叠／复制、机器人实收；U5–U7成本及30任务并发 |
| Web | 正式 Uvicorn／Chrome，用例各用替身或隔离真库 | 当前模型的真实任务质量和大量事实展示；无登录共用身份仍是已接受风险 |
| 发行／公司运行 | 手动 publish-amd64 发行；历史公司916f57a/schema16启动回报 | 当前主线7565c6a/schema17未在本任务发行／部署；服务器现状、故障恢复及用户接受不能从 healthy 推定 |

关键遗漏继续显式保留：

- StarRocks 审计时间按源写入时区，普通 naive DATETIME 不自动补偏移；公司目标 `CURDATE()/NOW()`、时区与既有展示口径须实测。当前窗口展示的北京时间口径限制及旧历史错误偏移不能靠排版追问自动重取／改写。
- SDK 回答改写失败可能被安全日志归为 `sdk_configuration`、用户仍见 `model_failed`；OpenAI 协议族 `conversation_locked` 内置重试不经过应用429/503策略，Vertex不触发。这些记录未被本次文档修订关闭。
- Grafana 数据源前后摘要检查不等于原子版本控制；Annotation 回读 limit=100 边缘未关闭。未知插件普通字段中的凭据识别缺口、写端令牌范围按已有取舍，不新增插件白名单。
- 单大字段超限可能没有可展示预览；缺失格式／指标不能由模型补造。旧 Grafana 群重发整文件和 C8 取消落定的偶发失败留在版本记录，本轮未复现，也不根据单次重跑认定根因关闭。
- 公司安全巡检 A0/A3、历史 H3／成文 I3、通知代理 openUrl／不带 at 的兼容和漏查目标检测的既有延期保持；任务质量、治理、源端能力、实收分别验收。

## 5. 验证记录与限制

### 当前产品基线的完整 CI

[PR #145](https://github.com/shixian66/xiaowei-agent-sdk/pull/145) 的 [CI #421](https://github.com/shixian66/xiaowei-agent-sdk/actions/runs/40721039641)，精确 head `bba19d4ba72c43524211f5afb523b70862ba68f7`，9/9 成功、无重跑；本轮只读复核 job 状态，未重跑。

| 检查 | 原运行结果 |
| --- | --- |
| 非部署集成 `python -m pytest -q --ignore=tests/deployment --durations=30` | 5511 passed、235 skipped、100 deselected、5 warnings，1348.29秒 |
| 发行／迁移／隔离恢复 `python -m pytest -q tests/deployment --durations=30` | 84 passed，566.47秒 |
| 普通测试 / 安全门 | 1461 passed / 65 passed；与集成集有重叠，不相加 |
| lint、types、product-entry、deps-audit、secret-scan | 全部成功；mypy44源码文件，锁定运行依赖 wheel 入口及全历史扫描 |
| 本地 Chrome 正式 DS 入口 | 同一最终head，2 passed，4.13秒；模型／DS为替身 |

CI 实际测试 PR 合并提交 `52911c87e7e9adb9fd35c2c86caf2c03b6de5adc`，tree `dd79cdf2ca4c8a9bb681826d138f2e498b57e090`；与受审 head 及主线 squash `7565c6a` 相同。对应源码已经历 R1 与 C1/C2 独立复审；C1/C2 源码候选的相关620 passed／1 skipped／1 deselected及定向15项仅作局部证据，不与CI拼接成新全量。

有效本地日志为 `/private/tmp/ds-c1c2-{related,final-browser}.log` 和独立复审 `/private/tmp/ds-c1c2-review-b469b5b.md`；这些是本机临时材料，不是仓库附件。可持续访问的版本与 CI 证据以 PR／运行链接为准，跳过或未选用例不计验收。

### 本次四文档对齐

只改 ARCHITECTURE、AGENT_HANDOFF、README、DEVELOPMENT_PLAN，产品 src/tests、配置、依赖、迁移和 CI 不改。按主线源码核对配置／SDK调用、Session与源绑定、数据库方言、DS／监控失败、历史快照、审批与交付、实例锁及发行路径；按 Git／GitHub 修正合入状态。

检查命令 `.venv/bin/python -m pytest -q tests/security/test_docs_command_consistency.py tests/security/test_workflow_policy.py -p no:cacheprovider`：**30 passed**；临时检查脚本核对四文档的188处出链、仓内163处入链及其中208处锚点，DS的19个变体编号完整，错误为0；`git diff --check` 通过。两个正式入口的 `--help` 均以0退出，不加载业务配置或连接真实服务。测试日志在 `/private/tmp/xw-root-docs-tests.log`；链接检查脚本在 `/private/tmp/xw_check_root_docs.py`，仅作本机复核材料，不新增仓库工具或测试。产品 src/tests 无变化，未重复执行产品全量、真实模型或公司服务检查；既有完整 CI 不冒充本次文档检查。

### 历史证据的保存方式

逐轮 RED→GREEN、失败与复审过程不在当前 handoff 重复追加。需要核对旧结果时，读取[本次整理前的固定快照](https://github.com/shixian66/xiaowei-agent-sdk/blob/7565c6a73e863fd791878990d0fd23bc3f33560e/AGENT_HANDOFF.md)及相应 PR／切片计划；其中“当前／下一项／未合入”等词只属于当时记录，现状以上文合入索引为准。更早证据可用 `git show 7e66718:AGENT_HANDOFF.md` 追溯。

未随 Git 保存的 `.superpowers/sdd/` 或已消失的临时日志不能作新的通过依据。文档一致、CI 通过和源码审查均不代替真实 Agent、公司 API、服务端负载、飞书实收或用户验收；本次不改变任何既有实战门槛。
