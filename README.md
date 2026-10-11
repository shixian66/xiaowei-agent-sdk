# 小维 · StarRocks 数据库助手

小维是一款以 **OpenAI Agents SDK** 为核心设计的数据库助手。首版目标是在 **Web 对话和飞书单聊** 中完成“查结构 → 只读查询 → 解释结果 → 分析慢查询”的连续对话。

> 下文描述首版目标，不代表仓库已经具备全部能力。当前实现、可运行入口和验证状态统一见 [AGENT_HANDOFF.md](AGENT_HANDOFF.md)。

**OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**

## 首版目标

- 了解获准 StarRocks 数据库的表、字段和结构。
- 用自然语言生成 SQL，或提交自己的 SQL，在受控范围执行只读查询并解释结果。
- 继续追问“这条 SQL 为什么慢”，查看估算执行计划（不执行原查询），得到有依据的优化建议与局限说明。
- 在 Web 或飞书连续对话，查看实际 SQL、有限结果和诊断依据。

查询与诊断共用一个业务 Agent。StarRocks 通过受治理 function tools 和本地 Adapter 直连；数据库 MCP 暂缓。通用 MCP Client Integration 保留用于将来其他能力；两条工具路径均复核权限、过滤结果并验证 Evidence。

**P2.5 与后续增量：** P2.5 开放账号实际 SELECT 范围、复杂 SQL、多集群和单 Agent 自然语言工具选择；随后增加指定飞书群共享与排队，均在 P3 前完成。产品边界见 [ARCHITECTURE](ARCHITECTURE.md#p25-scope)，实施顺序见 [DEVELOPMENT_PLAN](DEVELOPMENT_PLAN.md#6-p25-与飞书单群增量)。下文命令、配置和诊断用法描述当前主线；配置已改为 `targets` 多目标格式（P2.5 Task 1），数据范围改为只读账号实际可 SELECT 的对象（P2.5 Task 2）；跨库与复杂 SQL 已实现（P2.5 Task 3），搜表分页与多库慢查询（Task 6）、普通消息由单 Agent 判断是否查询（Task 7）均已合入。原 P2.5 Task 4 已取消，查询沿用权限、SQLGuard 和运行限额，EXPLAIN LOGICAL 用于按需诊断（见上述产品边界）。P2.5 的离线与隔离服务证据已齐（Task 8，见 AGENT_HANDOFF）；真实模型、用户 StarRocks 与飞书的实战验证在 P3。

**P4 监控增量（分片开发中）：** 同一 Agent 自主调查获准监控源。R1a/R1b、只读故障续查及 P2 已合入（PR #67/#68/#70/#71）：Prometheus 指标/标签/元数据/已加载规则发现、范围查询和有限 PromQL 修正。G3 已随 PR #72 合入，增加 Grafana 仪表盘搜索、摘要、面板原始表达式、数据源目录和标记读取；定义与实时指标分列，不执行 Grafana 代理查询。复用正式 `serve` 与 Web/飞书入口，接口与限制见[监控计划 §3.1–§3.4](docs/superpowers/plans/2026-10-09-monitoring-mcp.md)。离线实现不证明真实模型调查能力或公司监控环境已验收；A4 已随 PR #73 合入，以 API v2 直连外部独立 Alertmanager，读取告警/分组/静默/接收器/状态并复用同一治理与证据；W5 群审批底座候选用合成动作离线验证，实际监控写工具尚未开放；群流程与迁移见[运维说明](deploy/OPERATIONS.md#监控群审批底座)。产品与权限边界见 [监控接入设计](ARCHITECTURE.md#monitoring-mcp)，当前证据见 [handoff](AGENT_HANDOFF.md)。主配置样例仍不启用监控源；选配方式见[Prometheus](deploy/OPERATIONS.md#选配-prometheus-监控源)、[Grafana](deploy/OPERATIONS.md#选配-grafana-监控源)与[Alertmanager 运维说明](deploy/OPERATIONS.md#选配-alertmanager-监控源)。

## 最小产品形态

| 项目 | 首版设计 |
| --- | --- |
| Agent 核心 | OpenAI Agents SDK 的 Agent、Runner、function tools、Session |
| 模型 API | 计划接入 OpenAI、Gemini、DeepSeek 与 Vertex AI（API Key 模式）；通过配置选择经过验证的端点和模型，一次运行使用一个模型 |
| Web | 简单对话页：原生启动默认只监听本机（`listen_host` 可改）；Compose 部署默认发布到宿主机所有地址（`XW_WEB_BIND_ADDRESS=0.0.0.0`），供公司内网访问；无登录，访问者共用一个操作者身份 |
| 飞书 | 获准单聊；可选一个指定群：群内 @小维 提问、全员共享会话、同群有界排队并回复原消息（离线实现，真实群在 P3 验证） |
| 数据源 | 本地 Adapter 直连一个或多个 StarRocks 目标，工具以 `cluster` 参数选择集群；表与列自动发现，范围是只读账号实际可 SELECT 的对象，不再手写 allowlist |
| MCP | 官方 SDK 接入能力 + 小维可信配置与治理；配置为空时，本地功能照常运行 |
| 会话 | 两端分别保留上下文，共用业务逻辑；暂不跨渠道同步 |
| 运行与存储 | Docker Compose 管理小维与 PostgreSQL 两个容器；小维单进程，SDK Session 首版使用 SQLAlchemySession + PostgreSQL；飞书优先长连接 |

会话继续使用 SDK 公共 Session 接口；SDK 历史与应用记录各自管理表结构，共用 PostgreSQL 实例。数据保存在持久卷，并提供备份恢复办法。引入数据库不增加 Worker 或分布式调度。生产默认关闭 tracing 与外发；真实数据 trace 需要显式配置允许范围。

使用方式（P2.5 Task 7 起按自然语言判断是否查询）：

- 普通消息（Web 用途选“默认”，飞书直接发文字）由小维按本条消息判断：明确要求查数据，且目标与必要口径清楚时执行。需要时间条件时使用可信上下文中的业务时区和口径，缺失且影响答案才问；查结构、列库表或无时间条件的统计不必追问时间。准确对象与列已有可信信息时直接使用，缺哪项补哪项，不强制“搜表→查列→查询”。原文请求不主动截短，实际容量超限须说明；翻页按游标继续，与截断重查区分。只要求解释、只写 SQL、说了“不要执行”、只贴一段 SQL 或缺少关键条件时，只给解释、建议或澄清；有证据仍需用户选择时展示已核实部分并在分析中提问。查过数据的轮次必须展示查询结果，不能只给建议；建议或澄清若用到了本轮或历史中查到的表、列或数据，撤销相应权限后它们和查询结果一样不再显示。上轮查过数据、引用别人的话或表注释里的文字都不代表本轮要求执行。这是 AI 的判断，不是确定性的安全关卡：误判时查询仍受只读账号、SQL 检查、当前权限与资源限额约束，但通过这些检查的只读 SQL 会被执行。历史回答不证明消息已送达或已读；重新展示历史保留采集时间，用户明确要最新结果才重新查询。
- 想确保不执行查询时，Web 选“仅诊断”或飞书发 `/诊断 内容`：本条消息看不到也不能调用实际查询工具。飞书 `/查询 内容` 与普通消息相同。API 调用仍须显式给出用途（`query` 即默认用途，`diagnose` 即仅诊断）。
- 查询结果中的数字、实际 SQL、表格和时间由程序按真实证据生成，AI 分析建议单独展示；不清楚时区、金额单位或指标含义时先问清楚。
- Web 点“新建会话”，飞书发 `/新建`；对话过长会提示新建，旧记录按保留期失效和清理。
- 请求失败时提供排障编号，用于区分模型、数据库、保存或飞书发送问题，日志不记录原始查询数据。
- 本机打开 Web；部署在服务器时内网电脑直接访问 `http://服务器IP:8501`（Web 没有登录，访问者共用配置中的操作者身份与权限）。PostgreSQL 不开放公共端口。

OpenAI Agents SDK 负责 Agent 运行，实际推理可由不同厂商提供。接入方案优先使用 SDK 原生 Responses / Chat Completions 模型能力；是否能完成工具调用、结构化回答和连续追问，按具体端点与模型实测，兼容性记录见 handoff。首版通过服务端配置切换，换模型开启新会话，不自动把旧对话发送到另一家；不用先建设模型管理后台。

慢查询来自目标上已有的 AuditLoader 审计表，按环境能力配置（见下文“审计源”）；Query Profile 暂不接入。缺少审计源时，仍可分析 SQL 与执行计划，但必须明确证据不足；不会自动修改目标配置来开启采集。

MCP 负责标准化工具接入，不能替代业务授权。只有参数含义、风险和结果策略已有支持的服务，才能主要通过配置注册；并非填写一个地址就可安全使用任意工具。

首版不执行数据修改、配置变更或优化建议；不建设复杂管理后台、多 Agent 或分布式任务平台。

未来接入生产变更时，数据库修改、配置调整、取消查询、重启与发布统一按具体 Action 管理。所有生产写操作都先展示目标、改动和影响，取得有审批权限的用户明确确认，再复核权限后执行；目标或关键参数变化需要重新确认。“帮我诊断/优化”不代表同意执行生产变更。

## 怎样开始

唯一正式命令是 `xiaowei`（`python -m xiaowei` 相同）。它已用测试 PostgreSQL、脚本模型与替身完成离线验证；P3-A 和 P3-B 已通过独立审查并合入，升级、回退和隔离恢复在本机 Linux/arm64 容器环境验证。amd64 发行包由手动发布流程生成，应用镜像以离线文件随包交付；真实模型、用户 StarRocks、真实飞书与公司服务器实战仍未完成（见 handoff）。旧 CLI、Worker 或旧 Compose 命令不是产品入口。

实施时需要：

1. 开发使用 Python 3.11 与 uv，部署使用 Docker 和 Docker Compose；新依赖和镜像版本验证后锁定。P1-A 的存储验证也使用隔离的真实 PostgreSQL。
2. 一家获准的模型服务：模板默认 Vertex AI（API Key 模式，只需模型 ID 和 Key，地址与协议由程序固定）；也可用 OpenAI、Gemini、DeepSeek 或兼容网关，此时须明确 API 地址、协议和模型 ID。Key 都只写本机安全引用。
3. 获准的 StarRocks 测试连接、真实只读账号、数据库/视图范围、简短业务口径说明，以及允许向模型与渠道展示的数据。
4. 飞书企业自建机器人、消息权限、事件订阅及获准单聊用户；启用指定群时另需机器人入群，以及群内 @ 消息事件、获取群成员与回复消息的权限（见[单群计划 F0 证据 7](docs/superpowers/plans/2026-10-03-feishu-group.md)）。

凭据仅在本机或部署环境配置，不粘贴到对话、仓库、浏览器或日志。缺少真实环境时可以开发和离线验证，但不能标记对应实战验收完成。

**正式命令。** 服务器首次安装按 [运维说明的“首次安装”](deploy/OPERATIONS.md#首次安装)：复制两份模板、填写环境变量并核对目标，再依次 `config check` → `model check` → 初始化 → 启动。原生运行同样从 [examples/xiaowei.example.json](examples/xiaowei.example.json) 开始：它含用户批准的公司 `fat` 集群与 Vertex 模型非密钥配置；在其他环境使用时必须先改目标、模型和 TLS 设置。改用其他供应商时 `model` 段另填 `base_url`、`api_mode` 与 `output_mode`。凭据只写 `env:NAME` 引用，变量在运行环境中设置：

```bash
uv sync --locked
export XW_DATABASE_URL=...   # postgresql+asyncpg://...，以及 XW_DIGEST_KEY、XW_MODEL_API_KEY 和每个集群的 StarRocks 口令（模板只有 XW_STARROCKS_PASSWORD）
uv run --locked xiaowei --config xiaowei.json config check      # 离线核对配置、环境引用和本地 CA
uv run --locked xiaowei --config xiaowei.json model check       # 只需模型 Key；向模型发一次合成工具往返，产生用量
uv run --locked xiaowei --config xiaowei.json storage init      # 全新数据库
uv run --locked xiaowei --config xiaowei.json storage upgrade --bind-existing-digest-key  # v1–v5；仅确认原密钥并备份后
uv run --locked xiaowei --config xiaowei.json serve             # Web 默认 http://127.0.0.1:8501，Ctrl-C 停止
uv run --locked xiaowei --config xiaowei.json storage cleanup --batch-size 100
uv run --locked xiaowei --config xiaowei.json requests resend --subject <subject> --chat <chat_id> --message <message_id>
uv run --locked xiaowei --config xiaowei.json requests resend --subject <发起人 open_id> --group --message <message_id>
```

`serve` 持有数据库实例锁并在启动时执行中断恢复，第二个实例会被拒绝；普通启动不建表、不升级。`requests resend` 只重发飞书中投递为 failed/unknown 的已保存结果，不重跑模型或查询；发送前按当前数据库权限与对象版本复核结果引用的 StarRocks 对象（只做零行探测与元数据读取），因此需要与 `serve` 相同的 StarRocks 密码环境变量和网络可达。目标暂时连不上时不发送、记录保持可再次重发；确定撤权或对象已变化时拒绝。退出码：0 成功，1 运行失败或请求被拒，2 参数或配置错误。Web 只提供 HTTP；`web.allowed_origins` 为旧配置兼容字段，当前不限制 Host/Origin，访问范围由部署网络控制。持锁的数据库连接一旦断开，进程立即停止接收并以 1 退出；新实例接管时会等待旧实例仍在提交的请求接收，再执行恢复。正式日志只输出小维自身日志（级别由 `--log-level` 决定），依赖库的日志在任何级别都不输出。

飞书配置：主模板的 `feishu` 为 `null`；启用时从下方飞书段示例复制配置。`feishu.domain` 只接受 `"feishu"` 和 `"lark"`：应用后台网址是 `open.feishu.cn` 时选 `"feishu"`，是 `open.larksuite.com` 时选 `"lark"`；省略时默认 `"feishu"`。

**飞书指定群（可选）。** 含指定群的完整 `feishu` 段见 [examples/feishu-group.example.json](examples/feishu-group.example.json)，放进主配置的 `feishu` 键后按获准环境替换占位值；示例的单聊名单 `users` 为空：此时单聊没有人获得权限，未登记的人私聊机器人会收到一条只含其本人 `open_id` 的固定提示，用来交给管理员登记；登记单聊用户时须同时在 `access.grants` 给其 subject 非空工具，否则 `config check` 与启动都会拒绝（见[运维说明的“启用飞书”](deploy/OPERATIONS.md#启用飞书)）。群成员不需要出现在 `users` 中。在 `feishu` 段加入 `group`：`chat_id`（`oc_` 开头）、`tools`（全员同权共享的提出/读取工具，只能是已登记工具；实际写工具不放这里）、`member_page_size`（≤100）、`member_max_pages`（≤20）、`member_timeout_seconds`（≤30），以及同群排队的 `max_waiting`（等待条数上限，只不计正在运行的一条，已轮到但还在等处理能力的一条也计入；≤100 且不超过 `queue_size`）、`max_wait_seconds`（从接受起的最长等待，须短于 `storage.request_retention_seconds`）与 `wait_check_seconds`（到期检查间隔，≤60 且不超过 `max_wait_seconds`）。启用指定群时 `feishu.stop_timeout_seconds` 不得小于 7（飞书 SDK 关闭时可能固定等待约 6 s）。只处理这个群里 @小维 的文字消息：按平台 mention 中机器人的 `open_id` 识别（启动时由 SDK 取得，取不到时群消息一律不处理）；只写“@小维”文字、@其他机器人、其他群或机器人发的消息都不处理。群成员不需要出现在单聊 `users` 中，在群里能用也不代表能用单聊或 Web。群内成员共享一个会话：B 可以追问 A 刚才的结果，`/新建` 对全群生效；交给模型的每条消息首行带一个由发送者算出的短标识（不含 `open_id`），用于区分谁问的，不赋予任何权限。每轮开始运行前和每次发送前都经飞书接口重新确认发起人仍在群里；已取到的名单（群规模超过页数上限时只是部分名单）中找不到发起人、接口失败或超时都按无法确认处理，本轮不执行或不发送，也不发提示；群规模较大时需相应调高页数上限。回复一律回复原消息；原消息已撤回或不可见时记为发送失败，不会改发新消息或发到别处。发出的文字中的 `<at …>` 改为全角，回答不能 @ 任何人。群结果重发用 `--group` 并给出原发起人的 `open_id`，只回复原消息，发送前同样重新确认成员身份。同群同一时刻只运行一轮：其余提问按小维接受的顺序排队（不按飞书消息时间），新排队的提问会收到一次“已收到，前面还有问题在处理，将按顺序回复”，前一轮的结果保存并发送一次后才开始下一轮，下一轮能看到上一轮的结果；排队不占用单聊或 Web 的处理能力。排队提示的发送结束之前，小维不会开始运行这条提问，也不会发出它的繁忙回复；这只保证小维这边的发送先后，飞书上显示的先后仍受网络、发送期限和结果不明的情况影响。等待提示发送时不占用单聊或 Web 的处理能力。排队已满或等待超过 `max_wait_seconds` 的提问记为“系统繁忙”并回复原消息，不会执行，可稍后重新发问；尚未开始运行的提问（包括已轮到、但还在等处理能力的下一条）都计入排队上限并按这个期限处理。`wait_check_seconds` 只决定多久发现并记下到期，回复何时到达还取决于发送前的成员确认、网络与发送期限，到期回复尚未发完的提问仍计入排队上限；这期间飞书重复投递同一提问不会再确认成员或重复回复。排队或运行中发 `/新建` 会被拒绝。重启时只在排队、尚未开始的提问记为中断，群会话保持可用；已开始运行的一轮被中断时，群会话照常关闭，需 `/新建`。飞书入站不再请求通讯录接口（单聊同样）。

**诊断。** 普通消息即可提问诊断；想确保不执行查询时 Web 选“仅诊断”或飞书用 `/诊断`：可以粘贴 SQL、问“刚才那条为什么慢”，或在配置了审计源时问“最近一小时最慢的查询”。显式诊断轮只能查看表结构、表布局（表模型、分区、分桶、排序键）、执行计划与审计慢查询，看不到也不能调用实际查询工具。执行计划只用固定级别 `EXPLAIN LOGICAL` 获取，不执行原查询，不包含实际耗时；EXPLAIN ANALYZE 不开放。回答中的工具结果由程序生成并附固定说明（如“计划是估算”“审计有导入延迟”），分析建议单列为模型推断，优化后的 SQL 只是建议、不会被执行。

飞书有证据的回答使用一张静态卡片，来源、采集时间、必要说明与事实默认可见，分析在事实后显示；简短建议和澄清保留真实换行。超容量优先保留必要说明、实际业务 SQL 和整行事实，再放分析；未展示的事实、分析或 SQL 分别标明。实际 SQL 与其他技术详情可折叠；DDL 原文直接显示。JSON 2.0 卡片要求客户端至少 7.20；容量与发送状态的限制见[运维说明](deploy/OPERATIONS.md#飞书结果展示与容量)。本机测试和预览不证明真实 Lark 客户端效果。

**目标（`targets`）。** 每项为 `type`（目前只能是 `starrocks`）、`description`（集群用途，交给模型选择集群）、`business_context`（该集群的业务口径 `{"version", "text"}`，没有则写 `null`）与 `starrocks`（连接、限额、函数闭集与结构快照上限）。集群 ID 就是 `starrocks.target_id`，只能用 1–32 位小写字母、数字、`_`、`-`，不能重复；连接地址、账号与凭据引用不交给模型。授权表按工具授予，获准用户可用全部已配置集群；模型每次调用数据工具都必须给出 `cluster`，未知集群在连接前拒绝，不会改查其他集群。旧版顶层 `starrocks`/`business_context` 会在启动时报错并给出迁移方式：把原 `starrocks` 放进 `targets` 的一项，原 `business_context` 去掉 `target_id` 后放到同一项。新增、删除集群或修改口径后，旧会话需要新建；已保存的 StarRocks 结果在本次升级后一次失效。

**SQL 语义与升级。** 小维连接固定 SQL 模式，`||` 表示逻辑 OR；完整约定见 [ARCHITECTURE §6](ARCHITECTURE.md#6-只读查询保护)。D1/D2 修复把 StarRocks 证据摘要升为 v3，P2.5 Task 3（跨库与复杂 SQL）再升为 v4，Task 6（搜表分页与多库慢查询）升为 v5；每次升级前保存的 StarRocks 证据一次性失效，引用它们的旧会话须新建并重新查询，历史与重发也不能继续交付旧事实。其他工具的证据指纹不变。P2.5 Task 7 起，每条回答随结果保存模型本轮看过的证据，交付时一并按当前权限复核；升级前保存的回答没有这项记录，历史读取与 `requests resend` 不再交付（包括澄清与建议），不需要迁移表结构。

**应用表版本 7、升级与恢复。** 版本 5 保存个人/群 owner 与群回复目的地；版本 6 把数据库与 `XW_DIGEST_KEY` 的不可逆指纹绑定；版本 7 新增监控 Action 表，会话历史表不变。`serve`、升级、清理和重发遇到错密钥都会在业务 I/O 前拒绝。v1–v5 只有在操作者确认 `.env` 仍是原部署密钥并已完成配套备份后，才可执行 `storage upgrade --bind-existing-digest-key`；程序不能替操作者证明旧密钥来源。v6 升到 v7 使用普通 `storage upgrade`，必须保留原密钥绑定，缺失或错配不会自动修补。普通启动不建表、不迁移，旧程序也拒绝新 schema。回退到不认识新 schema 的旧程序时，须使用旧镜像、旧配置和迁移前备份恢复出的隔离数据库，不在原库降级。容器首次安装、普通升级/回退、原子 `pg_dump`、隔离 `pg_restore`、主机重启后的启动和排障命令统一见 [容器运维说明](deploy/OPERATIONS.md)。运行镜像与发行归档不包含根文档、源码测试或开发工具。

**数据范围（自动发现）。** 不再配置表与列：小维每隔 `schema_limits.refresh_seconds` 读取一次 `information_schema` 中全部用户库的表、视图与列，并对每个对象做一次不返回数据的 `SELECT 1 … WHERE 1 = 0` 探测，只有只读账号确实能 SELECT 的对象才进入范围。查询与执行计划可引用其中任何库的对象并跨库 JOIN：表名写成 `库名.表名`，只在一个库中存在的表可以省略库名（不使用连接的默认库猜测；多个库都有同名表时要求写明库名）。`list_tables`、`describe_table`、`describe_table_layout` 在返回前会再次确认对象仍可读。

**列库（`list_databases`）。** 分页列出当前账号有可查询表或视图的数据库，只返回库名；不含空库、系统库或没有可读对象的库。每库确认至少一个对象当前可读，交付与历史读取仍复核该对象权限。每页最多探测 32 个候选对象；撤权库对象较多时可能出现空页但 `next_cursor` 非空，须继续取到 null 才算列完。页大小为 1 到 `policy.max_rows`。新工具需同时加入 `data_policy.model_tools` 和对应 `access.grants`（群还需加入 `feishu.group.tools`），现有部署不会自动获得该权限；示例配置已列出。

**搜表与列某库全部表（`list_tables`）。** 有关键词时，不区分大小写匹配“库名.表名”、表注释（按 `max_comment_chars` 截取）与列名，`database=null` 搜索全部库；要列出指定库的所有表，传 `database` 和 `keyword=null`。按库表名排序分页，`page_size` 为 1 到 `policy.max_rows`，结果字节上限可能令一页少于它；每页只确认本页对象的当前权限，撤权对象不列出。第一页 `cursor=null`，之后原样回传 `next_cursor` 并保持其他参数不变；取到 null 才列完，预算不够时须说明仍有未列出的结果。目录内容不变的定时刷新可以继续翻页；内容或参数改变后旧游标在 I/O 前拒绝。单个对象（含表注释）超过容量时拒绝，不能静默跳过。

**表结构（`describe_table`）。** 返回字段、完整类型（保留长度与精度）、是否可空和注释。列多时通过 `next_cursor` 分次取得，每次重验权限，目录内容改变后旧游标失效；单列超过容量时拒绝。主键与排序键须另查布局，不能凭字段名猜测；布局不等于原始 SHOW CREATE TABLE。桶数为 0 只报告元数据原值，不据此确认实际桶数或自动扩缩容。

**内部表原始 DDL（`show_create_table`）。** 直接返回普通内部表的 SHOW CREATE TABLE 原文，支持本地 `OLAP` 与存算分离 `CLOUD_NATIVE` 引擎，保留默认值、完整键、分区定义与表属性，不执行 CREATE。读取前后核对对象 ID、类型、引擎；记录、发送和历史读取仍复核当前 SELECT 权限、对象 ID 及采集时列的类型。视图、物化视图、外表和外部 catalog 尚未开放。服务器可能规范化语句并补默认属性，因此原文不保证等于最初输入；分区定义也不等于当前分区状态列表。历史 DDL 是采集时的定义：新增列、表属性或分区变化不会自动使旧原文失效，旧原文会带采集时间显示；要看现状请重新查询。

该工具需独立加入 `data_policy.model_tools` 与使用者的 `access.grants`，已有授权不会自动扩大。示例仅给 Web 使用者配置此能力，飞书群示例未加入。每个目标可配置 `starrocks.max_ddl_bytes`（JSON 编码字节数），未配置时沿用 `max_value_bytes`；仍须满足 `max_result_bytes` 与四种投影容量。示例 DDL 上限 64000 不是所有表通用的值；显式日分区很多的表可能超过，升级前须用目标集群的代表性 SHOW CREATE 原文测量 JSON 编码字节数，并核对结果及四种投影容量。普通查询单值上限示例为 4000 字节，超限值带截短标记并继续返回放得下的行；DDL 超限则整条原文不返回，不裁成半段后声称完整。

**Web 实战修复升级。** StarRocks 证据范围摘要升至 v7（独立于应用表版本），旧 StarRocks 证据一次性失效，相关旧会话须新建；该修复不迁移应用表。Web 主回复中的事实表格只显示一次，SQL、游标、来源与本次获准结果原文可展开；采集时间、固定说明与普通查询的模型分析默认可见。澄清和未执行的 SQL 建议保留原换行。表格保留未截短值的大整数精度，区分真正的 NULL、字符串 `"NULL"`、空字符串与未提供字段；嵌套字段完整保留。原文仅包含本次获准展示的数据；超长单值只保留前缀与截短标记，无法从展示恢复原值或原类型，分页或截断也不等于已取全。“已回复”表示已交付，不代表每项业务需求均已满足。每轮向模型提供当前 UTC 时间，相对日期仍须结合明确的业务时区。

**SQL 写法（`run_readonly_query` / `explain_query`）。** 单条只读 SELECT 或 UNION/UNION ALL，可带非递归 WITH、子查询（含相关子查询）与窗口函数（PARTITION BY / ORDER BY，不支持窗口框架与命名窗口），函数须在 `allowed_functions` 中（窗口函数如 `ROW_NUMBER`、`RANK` 同样要列出）；名单按解析后的内部名填写，如 `IFNULL` 需要 `COALESCE`、`DATE_FORMAT` 需要 `TIME_TO_STR`、`DATE_TRUNC` 需要 `TIMESTAMP_TRUNC`，对照表与日期区间限制见[运维说明](deploy/OPERATIONS.md#常用上限与函数名单)。`*` 与 `别名.*` 按表结构展开，展开后的 SQL 同样受 `max_sql_bytes` 限制；查询结果最多 `max_result_columns` 列，列名不能重复，表达式列须用 `AS` 起别名（执行计划不要求）。列名不区分大小写，库名、表名与别名区分。名字按 StarRocks 的规则解析：WHERE、JOIN ON 与窗口中不能引用输出别名；ORDER BY 中的输出列名原样交给 StarRocks 解析，结果与直接执行原 SQL 相同（包括原 SQL 会报的错）；相关子查询中本层没有的列按外层表解析，多个外层表都有时需写明表名。

- `policy` 为 `allowed_functions`、`max_rows`、`max_sql_bytes` 与必填的 `max_result_columns`（P2.5 Task 3 新增，旧配置需补上）；旧版的 `allowed_objects`、`allowed_columns`、`target_id`、`default_database` 会在启动时报错，删除即可。
- `schema_limits`：`refresh_seconds` / `max_age_seconds` / `refresh_timeout_seconds`（代码默认 60 / 300 / 10 秒，主模板取 300 / 900 / 120；间隔不能大于最大年龄，刷新期限不能大于间隔）；`max_objects`、`max_columns`、`max_bytes`（每条元数据读取序列化后的字节上限）与 `max_comment_chars` 必须按目标规模填写，超过任一上限时这次刷新不生效。字段类型改为完整的 `COLUMN_TYPE` 后，长度和精度会增加列元数据字节数；升级前按目标集群的真实字段规模核对 `max_bytes` 和刷新期限。新快照超限时保留旧快照直到过期，随后该目标的数据工具不可用。
- 刷新失败时沿用上一份结构，直到它超过 `max_age_seconds`；之后该集群的数据工具暂不可用，其他集群不受影响。新授予的表在下一次成功刷新后可用；撤权后列表与表结构在返回前即发现，新查询由 StarRocks 拒绝。
- StarRocks 4.1.4 不支持列级授权：能读的表，其全部列都可能进入查询结果。需要隐藏列时，请 DBA 只授予只含允许列的视图。账号只靠未激活的角色获得权限时视为无权，需 DBA 设置默认角色或直接授权。
- 配置的审计源表不会进入查询范围，审计原文只通过 `list_slow_queries` 逐条检查后展示。
- 交付已保存的事实前，按当前权限逐个对象复核，次数受 `budget.max_scope_checks` 限制（必填，1–100000，旧配置需补上）。一轮对话中的历史回放、工具结果、最终校验与保存共用一个计数，每次复核同一集群的同一对象只算一次；轮次结束后的首次发送、历史读取与 `requests resend` 每次单独计数。用完后本次不交付，按“暂时无法确认”处理，会话与已保存结果保留，可新建会话或稍后重试。一轮新列表的每个对象要复核 5 次，因此该值不能小于各集群 `policy.max_rows` 的 5 倍；同一会话的历史越长，每轮需要的次数越多。

**审计源（可选）。** `targets[].starrocks.audit` 指向该集群上已有的 AuditLoader 审计表；小维不安装插件、不改其配置。各字段：

- `database` / `table`：审计库表名（官方默认 `starrocks_audit_db__` / `starrocks_audit_tbl__`），只读账号需要该表的 SELECT 权限。
- `time_zone`：必须等于 FE 服务器的系统时区（审计时间按它写入）。配错不会报错，但时间窗会偏移，结果为空或错位；示例中的 `Asia/Shanghai` 部署时按实际核对。
- `stmt_limit`：必填，填插件的 `max_stmt_length`，并要求 `policy.max_sql_bytes <= stmt_limit - 4`，保证读出的原文没有被插件截断。
- `max_window_minutes` / `max_rows` / `candidate_rows` / `candidate_bytes`：可查的最长时间窗、最多列出的行数，以及一次取出并检查的候选行数与字节上限。

没有任何集群配置 `audit` 时不提供 `list_slow_queries`，配置开放或授予它会在启动时报错；只给部分集群配置时，只有这些集群可以使用它，其他集群的调用在连接前拒绝。读取该集群所有库的审计记录，`database` 参数不为 null 时只读执行时当前库为它的记录。原文中未写库名的表按该记录执行时的当前库（结果中的 `database` 列）解析，与 StarRocks 执行时相同；当时没有当前库的记录只接受写明库名的原文。只列出原文引用的对象、列与函数全部获准的查询，列表可能少于上限，为空也不代表没有慢查询；结果标记截断表示列表不完整：候选读满 `candidate_rows` 仍有剩余、候选达到字节上限、已列满 `max_rows` 而还有未检查的候选（即使它们最终可能都不获准），或获准的记录因单值或结果字节上限没有列出。插件刚安装后首批记录可能缺失；审计表任一行格式异常（如时间为空、指标不是整数）时整个工具失败，而不是返回部分结果。候选读取与原文检查各有一个客户端期限，最坏总耗时约为 2 倍 `client_timeout_seconds`。

**开发验证**（只用合成数据、脚本模型、替身与隔离的测试 PostgreSQL，不连接任何真实模型或外部服务）：

```bash
uv sync --locked --extra dev
docker compose -p xiaowei-sdk-test -f compose.sdk-test.yml up -d --wait   # 或独立的 docker-compose
SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres \
  uv run --locked --extra dev python -m pytest tests/sdk_core tests/p1b -q
uv run --locked --extra dev ruff check src/xiaowei tests/sdk_core tests/p1b
uv run --locked --extra dev mypy src/xiaowei
docker compose -p xiaowei-sdk-test -f compose.sdk-test.yml down -v
```

`SDK_TEST_POSTGRES_URL` 只能是上面这一个测试地址；未设置时数据库用例明确失败而不是跳过。CI 的 integration job 以同样方式运行完整测试。

P3-A 与 P3-B 已经独立审查并合入；精简发行包、两容器 Compose、配置预检、首次初始化与停止步骤见 [deploy/OPERATIONS.md](deploy/OPERATIONS.md)。小维连接已有 StarRocks 和模型 API，不要求在服务器部署模型；应用配置、`.env`、CA 与 PostgreSQL 命名卷由操作者保留。amd64 发行包自带应用镜像文件（`docker load` 导入，不需要镜像仓库）；公司服务器与真实服务验收仍待 P3-C。旧根目录 Compose 文件仍属于历史实现。

## 设计与开发

采用 AI 辅助的小步开发：给出本次目标和完成判据，按当前计划实现、验证、审查、更新交接，再继续下一项。开发规则和风险分级统一见 [AGENTS.md](AGENTS.md)，不需要为每次改动重复填写五份文档。

换 AI 工具或新开任务时，明确要求它先按 AGENTS 的顺序读文档，并从 handoff 的下一项工作接续；不要假定它能看到旧对话。任务目标应写清是审查、修订计划还是实施代码。

- [AGENTS.md](AGENTS.md)：开发规则、SDK 优先原则与工程质量要求。
- [ARCHITECTURE.md](ARCHITECTURE.md)：完整产品设计、工具、双入口与执行边界。
- [AGENT_HANDOFF.md](AGENT_HANDOFF.md)：当前代码与已经验证的事实。
- [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)：逐步交付的顺序和验收目标。
- [P1-A 实施计划](docs/superpowers/plans/2026-09-29-p1a-sdk-governed-core.md)：先验证 SDK、模型 API、治理与 MCP 核心，再接真实数据库和双入口；当前执行到哪一步以 handoff 为准。
- [P1-B 实施计划](docs/superpowers/plans/2026-09-30-p1b-starrocks-dual-entry.md)：真实只读查询、请求状态、Web/飞书与正式入口的唯一详细切片计划；各切片的完成与验证状态以 handoff 为准。
- [监控 MCP 增量实施计划](docs/superpowers/plans/2026-10-09-monitoring-mcp.md)：P4 的协议门槛、读取/写入切片与真实环境验收；未完成项以 handoff 为准。

设计直接使用 [OpenAI Agents SDK](https://developers.openai.com/api/docs/guides/agents/sdk) 原生能力。旧实现只在有明确价值时提取少量业务素材，兼容旧框架不是新产品目标。

M5 是 Git 历史起点，不是必须保留的架构。仓库中尚未清理的旧代码、测试、ADR 和里程碑用于历史参考，不能与上述新设计共同作为新产品规范。
