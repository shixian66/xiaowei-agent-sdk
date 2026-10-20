# 小维 · StarRocks 数据库助手

小维是一款以 **OpenAI Agents SDK** 为核心设计的数据库助手。首版目标是在 **Web 对话和飞书单聊** 中完成“查结构 → 只读查询 → 解释结果 → 分析慢查询”的连续对话。

> 下文描述首版目标，不代表仓库已经具备全部能力。当前实现、可运行入口和验证状态统一见 [AGENT_HANDOFF.md](AGENT_HANDOFF.md)。

**OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**

## 首版目标

- 了解获准 StarRocks 数据库的表、字段和结构。
- 用自然语言生成 SQL，或提交自己的 SQL，在受控范围执行只读查询并解释结果。
- 继续追问“这条 SQL 为什么慢”，查看估算执行计划（不执行原查询），得到有依据的优化建议与局限说明。
- 在 Web 或飞书连续对话，查看实际 SQL、有限结果和诊断依据。

查询与诊断共用一个业务 Agent。StarRocks 通过受治理 function tools 和本地 Adapter 直连；数据库 MCP 暂缓。通用 MCP Client Integration 用于获准的监控等外部工具；两条工具路径均复核权限、过滤结果并验证 Evidence。

**P2.5 与后续增量：** P2.5 开放账号实际 SELECT 范围、复杂 SQL、多集群和单 Agent 自然语言工具选择；随后增加指定飞书群共享与排队，均在 P3 前完成。产品边界见 [ARCHITECTURE](ARCHITECTURE.md#p25-scope)，实施顺序见 [DEVELOPMENT_PLAN](DEVELOPMENT_PLAN.md#6-p25-与飞书单群增量)。下文命令、配置和诊断用法描述当前主线；配置已改为 `targets` 多目标格式（P2.5 Task 1），数据范围改为只读账号实际可 SELECT 的对象（P2.5 Task 2）；跨库与复杂 SQL 已实现（P2.5 Task 3），搜表分页与多库慢查询（Task 6）、普通消息由单 Agent 判断是否查询（Task 7）均已合入。原 P2.5 Task 4 已取消，查询沿用权限、SQLGuard 和运行限额，EXPLAIN LOGICAL 用于按需诊断（见上述产品边界）。P2.5 的离线与隔离服务证据已齐（Task 8，见 AGENT_HANDOFF）；真实模型、用户 StarRocks 与飞书的实战验证在 P3。

**监控能力（P4）。** 同一 Agent 可调查获准的 Prometheus 指标/标签/规则和范围结果、Grafana 仪表盘/面板/数据源/标记，以及独立 Alertmanager 的告警/分组/静默/接收器/状态；Grafana 定义与实时指标分开，不执行 Grafana 代理查询。指定群可选配逐次批准的静默创建/取消、经典 Dashboard 创建/修改、数据源非秘密字段创建/修改和 Annotation 创建，使用 SDK 原生暂停/恢复；这些实现不代表公司生产写已开放。数据源密码目前由管理员在 Grafana UI 配置，私聊密码命令尚未实现。主配置默认不启用监控源；选配、认证、迁移与开放门槛见 [Prometheus](deploy/OPERATIONS.md#选配-prometheus-监控源)、[Grafana](deploy/OPERATIONS.md#选配-grafana-监控源)、[Alertmanager](deploy/OPERATIONS.md#选配-alertmanager-监控源)、[Dashboard 写](deploy/OPERATIONS.md#选配经典-dashboard-创建和修改)和[群审批说明](deploy/OPERATIONS.md#监控群审批底座)。产品边界见 [ARCHITECTURE](ARCHITECTURE.md#monitoring-mcp)，合入版本、验证证据与缺口集中在 [handoff](AGENT_HANDOFF.md)。

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
- Web 点“新建会话”，飞书发 `/新建`；新建不等于立即删除保存记录。新失败会区分轮数、历史容量、过期、绑定变化和其他不可继续，历史显示的是本轮结束时状态；旧记录仍保留通用提示。提交容量超限会说明已执行工具不自动重试，旧记录按保留期失效和清理。迁移与排障见[会话拒绝说明](deploy/OPERATIONS.md#会话拒绝原因与历史快照)。
- 轮数或完整历史容量达到 80%，或距固定期限不超过 1 小时，成功回复会同条显示系统提醒；Web 与飞书均默认可见，多个条件合成一条。提醒是“本轮结束时”的观察，不保证还能聊多少次；最后合法提交会说明下轮需要新建。它不进入模型上下文，不自动新建或复制线索，详见[提前提醒](deploy/OPERATIONS.md#会话提前提醒)。
- 主模板允许最多 100 次成功提交的交互；历史字节、模型请求和 7 天固定期限仍独立限制，长结果可能提前触顶。已有部署需手动调整 `session_limits.max_history_turns`，镜像升级不改配置；见[会话轮数说明](deploy/OPERATIONS.md#会话轮数的手动配置)。容量证据只证明合成调用链，真实模型长排障能力未验证。
- 历史简短追问（如“刚才本次返回多少条”）可显示代码生成的行数摘要，来源、采集时间及限制仍可见；原文在容量允许时可展开，未展示会说明。要原文、具体字段、聚合值或最新结果时明确提出。摘要不压缩会话，也不把返回行数当全表总数；R3 的真实模型选择仍待获准环境验收。
- 请求失败时提供排障编号，用于区分模型、数据库、保存或飞书发送问题，日志不记录原始查询数据。
- 本机打开 Web；部署在服务器时内网电脑直接访问 `http://服务器IP:8501`（Web 没有登录，访问者共用配置中的操作者身份与权限）。PostgreSQL 不开放公共端口。

OpenAI Agents SDK 负责 Agent 运行，实际推理可由不同厂商提供。接入方案优先使用 SDK 原生 Responses / Chat Completions 模型能力；是否能完成工具调用、结构化回答和连续追问，按具体端点与模型实测，兼容性记录见 handoff。首版通过服务端配置切换，换模型开启新会话，不自动把旧对话发送到另一家；不用先建设模型管理后台。

慢查询来自目标上已有的 AuditLoader 审计表，按环境能力配置（见下文“审计源”）；Query Profile 暂不接入。缺少审计源时，仍可分析 SQL 与执行计划，但必须明确证据不足；不会自动修改目标配置来开启采集。

MCP 负责标准化工具接入，不能替代业务授权。只有参数含义、风险和结果策略已有支持的服务，才能主要通过配置注册；并非填写一个地址就可安全使用任意工具。

P1–P3 的 StarRocks 能力不执行数据修改、配置变更或优化建议；监控写仅限上文 P4 已批准的动作；不建设复杂管理后台、多 Agent 或分布式任务平台。

已接入的监控写按具体 Action 管理；其他数据库修改、配置调整、取消查询、重启与发布仍须单独批准范围。所有生产写操作都先展示目标、改动和影响，取得有审批权限的用户明确确认，再复核权限后执行；目标或关键参数变化需要重新确认。“帮我诊断/优化”不代表同意执行生产变更。

## 怎样开始

唯一正式命令是 `xiaowei`（`python -m xiaowei` 相同）。它已用测试 PostgreSQL、脚本模型与替身完成离线验证；P3-A 和 P3-B 已通过独立审查并合入，升级、回退和隔离恢复在本机 Linux/arm64 容器环境验证。amd64 发行包由手动发布流程生成，应用镜像以离线文件随包交付；真实模型、用户 StarRocks、真实飞书与公司服务器的实战验收仍未完成（见 handoff）。旧 CLI、Worker 或旧 Compose 命令不是产品入口。

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


**多集群提问。** 先告诉小维用哪个环境，例如“用 fat”或“查看 fat 的 data_center.quota_virtual_account_quasi_rt 结构”；没说环境时小维会先问，不会自己去各环境查找。选定后同一会话的追问沿用该环境，说“切到 pre”才换；只是问“pre 上也有吗？”时小维会先确认是否切换。群里成员选定的环境全群沿用，但每轮权限和执行意图仍按发起人独立复核；`/新建` 后需要重新选择。只写或解释 SQL、不需要查库时不用选环境。具体边界见 [R7 目标规则](ARCHITECTURE.md#r7-targets)。这是提示与工具说明的改进，执行层不会拦截模型选错环境；真实模型的遵守情况尚未评测。

**诊断。** 普通消息即可提问诊断；想确保不执行查询时 Web 选“仅诊断”或飞书用 `/诊断`：可以粘贴 SQL、问“刚才那条为什么慢”，或在配置了审计源时问“最近一小时最慢的查询”。显式诊断轮只能查看表结构、表布局（表模型、分区、分桶、排序键）、执行计划与审计慢查询，看不到也不能调用实际查询工具。执行计划只用固定级别 `EXPLAIN LOGICAL` 获取，不执行原查询，不包含实际耗时；EXPLAIN ANALYZE 不开放。回答中的工具结果由程序生成并附固定说明（如“计划是估算”“审计有导入延迟”），分析建议单列为模型推断，优化后的 SQL 只是建议、不会被执行。

飞书普通解释、问候、建议和澄清优先使用官方 SDK 的受控结构化 Markdown，只启用标题、加粗、列表和换行。回答按模型给出的顺序排列文字与结果，动作材料置顶；普通正文用一次轻量 AI 标识，可信数据表与“查看依据”入口由代码生成，不机械追加“系统事实／其余结果／结果 N”标题。来源、采集时间、实际查询与完整原值在详情中可核对，内部证据编号不展示。影响结论的口径、实际查询窗口、警告和不完整状态在主视图保留简短提醒，完整说明仍放详情或明细文件；例如计划是估算、审计仅列获准候选、仪表盘配置不是实时指标、历史结果不代表当前状态。本轮 brief 收起但完整交付，历史 brief 保留摘要语义，未展示原文时明确提示。审批材料保持专用完整组件。JSON 2.0 卡片要求客户端至少 7.20。容量不足改用完整文件加正文，不以截断冒充完整交付；使用及恢复见[运维说明](deploy/OPERATIONS.md#飞书结果展示与容量)。本地协议验证不能证明真实 Lark 客户端的排版和复制效果。

飞书结果按容量交付：短结果完整发送一条消息；完整结果放不进消息时，自动交付 MD 正文及完整证据明细文件，再发送消息。明确要求 HTML 时使用静态离线页面，原始证据默认在可展开详情；普通短回答不附文件。用户明确要解释或未执行建议的 HTML/MD 文件时，无业务事实也可按所选格式生成，不能因此多查数据。同时要求 MD 和 HTML 仍先询问格式。文件与正文分别保存平台回执，缺少任何必需部分都不记为完整送达。单文件超过 400,000 字节时按整行或章节拆成标号文件；单行、字段或章节仍放不下时给预览并说明未完整交付。生产审批卡独立先发，参考数据失败不影响已确认材料的批准。真实模型、飞书和客户端尚待验收；代码合入不代表已经部署。使用、留存和手动补发见[报告文件说明](deploy/OPERATIONS.md#飞书报告文件)，契约见[ARCHITECTURE §7](ARCHITECTURE.md#result-delivery)。

**自主调查与普通消息（本地候选）。** 连续模型段落使用轻量区域身份；本轮 brief 原位收起并完整交付，未引用的必交结果与普通 metadata 默认收起；历史 brief 保留计数摘要，未展示原文时明确提示，不自动导出历史原值。必要范围、数据源警告与截断仍可见。展开面板和附件保留获准内容，模型文字仍是分析或建议。代码草稿局部保持原文，其余段落继续排版。飞书进度在单聊约 2 秒、指定群约 5 秒后显示，只按真实状态变化尽力更新；短任务没有过程卡，异常可能留下旧卡，正式结果承担交付。真实 Agent、客户端折叠/复制、平台权限及限频由后续公司环境验收，详见[实施与验证](docs/superpowers/plans/2026-10-20-agent-autonomy-progress.md)。

**模型自主报告（本地候选）。** 显式报告由同一个 Agent 通过原有文字/事实块组织：MD 原生显示标题、列表和表格；HTML 支持独立片段和受限内联样式，分析与代码生成的可信事实就近排列。主消息提取可读预览，不发送布局源码；报告指南随包按飞书渠道加载，无 StarRocks 目标也可用。文字仍受 16,000 字节和模型输出 token 限制；脚本、外部资源和任意网页执行不开放。新回答按可信版本 3 保存，旧版本/已绑定附件保持原语义；不改模型输出 schema 或数据库 v12。离线 SDK/治理/存储及 Chrome 检查不能证明真实模型的报告质量、真实飞书实收或部署效果，见[实施计划](docs/superpowers/plans/2026-10-19-model-authored-reports.md)。

**监控曲线。** 暂不实现，监控结果以文本/表格交付。[计划 D4.0](docs/superpowers/plans/2026-10-14-result-delivery-documents.md#d40-协议前置结果与最小修订提案2026-10-15)保留已完成的协议核验、反例与候选路径对比，供以后明确重启时参考。

飞书默认不读取图片、文件或富文本 post：合法且当前获准的消息获得固定说明。可显式开启 `feishu.document_input_enabled` 接收 UTF-8 TXT/MD；截图还须同时开启 `feishu.image_input_enabled` 和 Vertex 模型 Profile 的 `image_input_enabled`，仅接收单张不超过 2 MiB 的 PNG/JPEG，每会话最多 12 张，后续提问回放原图。指定群仍要求同一条消息的平台 @本机器人及当前成员资格；未 @ 的附件、post 内的 at 标签不会触发。真实 Lark 图片事件、模型识图及 12 图请求尚未验收；配置、容量和回退见[运维说明](deploy/OPERATIONS.md#飞书-pngjpeg-输入)。

**目标（`targets`）。** 每项为 `type`（目前只能是 `starrocks`）、`description`（集群用途说明，帮助用户和模型识别环境；会原样交给模型，不代表默认或已选择，不要写“默认用此集群”“可跨环境查找”之类的话）、`business_context`（该集群的业务口径 `{"version", "text"}`，没有则写 `null`）与 `starrocks`（连接、限额、函数闭集与结构快照上限）。集群 ID 就是 `starrocks.target_id`，只能用 1–32 位小写字母、数字、`_`、`-`，不能重复；连接地址、账号与凭据引用不交给模型。授权表按工具授予，获准用户可用全部已配置集群；模型每次调用数据工具都必须给出 `cluster`，未知集群在连接前拒绝，不会改查其他集群。旧版顶层 `starrocks`/`business_context` 会在启动时报错并给出迁移方式：把原 `starrocks` 放进 `targets` 的一项，原 `business_context` 去掉 `target_id` 后放到同一项。新增、删除集群或修改口径后，旧会话需要新建；已保存的 StarRocks 结果在本次升级后一次失效。

**SQL 语义与升级。** 小维连接固定 SQL 模式，`||` 表示逻辑 OR；完整约定见 [ARCHITECTURE §6](ARCHITECTURE.md#6-只读查询保护)。D1/D2 修复把 StarRocks 证据摘要升为 v3，P2.5 Task 3（跨库与复杂 SQL）再升为 v4，Task 6（搜表分页与多库慢查询）升为 v5；每次升级前保存的 StarRocks 证据一次性失效，引用它们的旧会话须新建并重新查询，历史与重发也不能继续交付旧事实。其他工具的证据指纹不变。P2.5 Task 7 起，每条回答随结果保存模型本轮看过的证据，交付时一并按当前权限复核；升级前保存的回答没有这项记录，历史读取与 `requests resend` 不再交付（包括澄清与建议），不需要迁移表结构。

**应用表版本 12、升级与恢复。** 版本 5 保存个人/群 owner 与群回复目的地；版本 6 把数据库与 `XW_DIGEST_KEY` 的不可逆指纹绑定；版本 7 新增监控 Action 表；v8 新增请求会话状态，v9 新增飞书会话原图表，v10 保存 SDK 暂停/恢复检查点与专用 Action 作废原因，v11 新增原请求绑定的飞书报告文件表，v12 新增最小交付分项与父请求语义版本，SDK 历史表不变。v8/v9 的旧待批 Action 升级后作废、需重新提出，成功/未知事实保留。`serve`、升级、清理和重发遇到错密钥都会在业务 I/O 前拒绝。v1–v5 只有在操作者确认 `.env` 仍是原部署密钥并已完成配套备份后，才可执行 `storage upgrade --bind-existing-digest-key`；程序不能替操作者证明旧密钥来源。v6–v11 升到 v12 使用普通 `storage upgrade`，必须保留原密钥绑定，缺失或错配不会自动修补。普通启动不建表、不迁移，旧程序也拒绝新 schema。回退到不认识新 schema 的旧程序时，须使用旧镜像、旧配置和迁移前备份恢复出的隔离数据库，不在原库降级。容器首次安装、普通升级/回退、原子 `pg_dump`、隔离 `pg_restore`、主机重启后的启动和排障命令统一见 [容器运维说明](deploy/OPERATIONS.md)。运行镜像与发行归档不包含根文档、源码测试或开发工具。

**数据范围（自动发现）。** 不再配置表与列：小维每隔 `schema_limits.refresh_seconds` 读取一次 `information_schema` 中全部用户库的表、视图与列，并对每个对象做一次不返回数据的 `SELECT 1 … WHERE 1 = 0` 探测，只有只读账号确实能 SELECT 的对象才进入范围。查询与执行计划可引用其中任何库的对象并跨库 JOIN：工具 SQL 中的物理表必须写成 `库名.表名`，不使用连接默认库或唯一匹配隐式补全。用户可以在提问中简写；工具收到未限定表名时先拒绝并返回有限候选，唯一候选可由 Agent 补库名后重试并告知用户，多个候选须澄清。`list_tables`、`describe_table`、`describe_table_layout` 在返回前会再次确认对象仍可读。

**列库（`list_databases`）。** 分页列出当前账号有可查询表或视图的数据库，只返回库名；不含空库、系统库或没有可读对象的库。每库确认至少一个对象当前可读，交付与历史读取仍复核该对象权限。每页最多探测 32 个候选对象；撤权库对象较多时可能出现空页但 `next_cursor` 非空，须继续取到 null 才算列完。页大小为 1 到 `policy.max_rows`。新工具需同时加入 `data_policy.model_tools` 和对应 `access.grants`（群还需加入 `feishu.group.tools`），现有部署不会自动获得该权限；示例配置已列出。

**搜表与列某库全部表（`list_tables`）。** 有关键词时，不区分大小写匹配“库名.表名”、表注释（按 `max_comment_chars` 截取）与列名，`database=null` 搜索全部库；要列出指定库的所有表，传 `database` 和 `keyword=null`。按库表名排序分页，`page_size` 为 1 到 `policy.max_rows`，结果字节上限可能令一页少于它；每页只确认本页对象的当前权限，撤权对象不列出。第一页 `cursor=null`，之后原样回传 `next_cursor` 并保持其他参数不变；取到 null 才列完，预算不够时须说明仍有未列出的结果。目录内容不变的定时刷新可以继续翻页；内容或参数改变后旧游标在 I/O 前拒绝。单个对象（含表注释）超过容量时拒绝，不能静默跳过。

**表结构（`describe_table`）。** 返回字段、完整类型（保留长度与精度）、是否可空和注释。列多时通过 `next_cursor` 分次取得，每次重验权限，目录内容改变后旧游标失效；单列超过容量时拒绝。主键与排序键须另查布局，不能凭字段名猜测；布局不等于原始 SHOW CREATE TABLE。桶数为 0 只报告元数据原值，不据此确认实际桶数或自动扩缩容。

**内部表原始 DDL（`show_create_table`）。** 直接返回普通内部表的 SHOW CREATE TABLE 原文，支持本地 `OLAP` 与存算分离 `CLOUD_NATIVE` 引擎，保留默认值、完整键、分区定义与表属性，不执行 CREATE。读取前后核对对象 ID、类型、引擎；记录、发送和历史读取仍复核当前 SELECT 权限、对象 ID 及采集时列的类型。视图、物化视图、外表和外部 catalog 尚未开放。服务器可能规范化语句并补默认属性，因此原文不保证等于最初输入；分区定义也不等于当前分区状态列表。历史 DDL 是采集时的定义：新增列、表属性或分区变化不会自动使旧原文失效，旧原文会带采集时间显示；要看现状请重新查询。

该工具需独立加入 `data_policy.model_tools` 与使用者的 `access.grants`，已有授权不会自动扩大。示例仅给 Web 使用者配置此能力，飞书群示例未加入。每个目标可配置 `starrocks.max_ddl_bytes`（JSON 编码字节数），未配置时沿用 `max_value_bytes`；仍须满足 `max_result_bytes` 与四种投影容量。示例 DDL 上限 64000 不是所有表通用的值；显式日分区很多的表可能超过，升级前须用目标集群的代表性 SHOW CREATE 原文测量 JSON 编码字节数，并核对结果及四种投影容量。普通查询单值上限示例为 4000 字节，超限值带截短标记并继续返回放得下的行；DDL 超限则整条原文不返回，不裁成半段后声称完整。

**Web 实战修复升级。** StarRocks 证据范围摘要升至 v7（独立于应用表版本），旧 StarRocks 证据一次性失效，相关旧会话须新建；该修复不迁移应用表。Web 主回复中的事实表格只显示一次，SQL、游标、来源与本次获准结果原文可展开；来源、采集时间、技术参数和完整原值在详情核对，正文保留一次 AI 说明及必要限制。澄清和未执行的 SQL 建议保留原换行。表格保留未截短值的大整数精度，区分真正的 NULL、字符串 `"NULL"`、空字符串与未提供字段；嵌套字段完整保留。原文仅包含本次获准展示的数据；超长单值只保留前缀与截短标记，无法从展示恢复原值或原类型，分页或截断也不等于已取全。“已回复”表示已交付，不代表每项业务需求均已满足。每轮向模型提供当前 UTC 时间，相对日期仍须结合明确的业务时区。

**SQL 写法（`run_readonly_query` / `explain_query`）。** 单条只读 SELECT 或 UNION/UNION ALL，可带非递归 WITH、子查询（含相关子查询）与窗口函数（PARTITION BY / ORDER BY，不支持窗口框架与命名窗口），函数须在 `allowed_functions` 中（窗口函数如 `ROW_NUMBER`、`RANK` 同样要列出）；部分函数接受常用写法与解析后名称，如 `DATE_FORMAT` 或同时列出 `TIME_TO_STR`、`TS_OR_DS_TO_TIMESTAMP`，`IFNULL` 仍需 `COALESCE`，详见[运维说明](deploy/OPERATIONS.md#常用上限与函数名单)。物理表须写成 `库.表`；缺库名时工具返回尚未过期的可读结构快照中的有限候选，实际执行仍复核当前权限，一个库可补全后重试，多个库须先澄清。`list_slow_queries` 的 SQL 取计划时应按该行 `database` 补库名。SQLGuard 只检查原文，实际执行不按解析器重打印；查询唯一可能的文字编辑是去尾分号、追加或下调顶层 LIMIT，事实区展示实际 SQL、原 LIMIT 与执行 LIMIT。带格式的 `FROM_UNIXTIME` 可以按函数授权执行；注释须删除后重试，hint 仍拒绝。`*` 与 `别名.*` 不改写执行 SQL，但按快照展开来检查引用和列数；原文与预计列头受 `max_sql_bytes` 限制，查询结果最多 `max_result_columns` 列，顶层及实际返回列名不能重复，无别名表达式可用 StarRocks 的返回列名。列名不区分大小写，库名、表名与别名区分。WHERE、JOIN ON 与窗口不能引用输出别名；ORDER BY 原样由 StarRocks 解析；相关子查询中本层没有的列按外层表解析，多个外层表都有时需写明表名。S1 不升级 StarRocks 数据范围摘要 `/8`；旧普通查询证据因查询策略字段变化不可读，旧计划、结构与慢查询证据仍按现有权限复核。

- `policy` 为 `allowed_functions`、`max_rows`、`max_sql_bytes` 与必填的 `max_result_columns`（P2.5 Task 3 新增，旧配置需补上）；旧版的 `allowed_objects`、`allowed_columns`、`target_id`、`default_database` 会在启动时报错，删除即可。
- `schema_limits`：`refresh_seconds` / `max_age_seconds` / `refresh_timeout_seconds`（代码默认 60 / 300 / 10 秒，主模板取 300 / 900 / 120；间隔不能大于最大年龄，刷新期限不能大于间隔）；`max_objects`、`max_columns`、`max_bytes`（每条元数据读取序列化后的字节上限）与 `max_comment_chars` 必须按目标规模填写，超过任一上限时这次刷新不生效。字段类型改为完整的 `COLUMN_TYPE` 后，长度和精度会增加列元数据字节数；升级前按目标集群的真实字段规模核对 `max_bytes` 和刷新期限。新快照超限时保留旧快照直到过期，随后该目标的数据工具不可用。
- 刷新失败时沿用上一份结构，直到它超过 `max_age_seconds`；之后该集群的数据工具暂不可用，其他集群不受影响。新授予的表在下一次成功刷新后可用；撤权后列表与表结构在返回前即发现，新查询由 StarRocks 拒绝。
- StarRocks 4.1.4 不支持列级授权：能读的表，其全部列都可能进入查询结果。需要隐藏列时，请 DBA 只授予只含允许列的视图。账号只靠未激活的角色获得权限时视为无权，需 DBA 设置默认角色或直接授权。
- 配置的审计源表经零行权限探测后进入查询范围。用户要求精确日期、Top N、统计或字段时，Agent 可直接生成 SQL 用 `run_readonly_query`，无需用户先提供 SQL；`list_slow_queries` 仍是有界的快捷诊断，只列 `isQuery=1` 且原文通过检查的记录，不列身份字段或审计表自身读取，不能替代用户指定的 SQL。
- 交付已保存的事实前，按当前权限逐个对象复核，次数受 `budget.max_scope_checks` 限制（必填，1–100000，旧配置需补上）。一轮对话中的历史回放、工具结果、最终校验与保存共用一个计数，每次复核同一集群的同一对象只算一次；轮次结束后的首次发送、历史读取与 `requests resend` 每次单独计数。用完后本次不交付，按“暂时无法确认”处理，会话与已保存结果保留，可新建会话或稍后重试。一轮新列表的每个对象要复核 5 次，因此该值不能小于各集群 `policy.max_rows` 的 5 倍；同一会话的历史越长，每轮需要的次数越多。

**审计源（可选）。** `targets[].starrocks.audit` 指向该集群上已有的 AuditLoader 审计表；小维不安装插件、不改其配置。各字段：

审计表全部可读列（包括 `user`、`authorizedUser`、`clientIp`、`stmt`）按只读账号权限开放给获准的 Web／飞书私聊与群聊。结果可能进入模型供应商、Session 和最长 7 天的 Evidence；查询仍受 SQLGuard、行数、单值／总字节、超时、内存与交付时权限复核约束。审计表较大，建议用 `timestamp` 限定时间范围；`NOW()` 按连接时区计算，审计 `timestamp` 按 FE 时区写入，两者不同时比较会偏移。`stmt` 经普通查询返回时可能带截短标记；需要完整原文须检查该标记，不可把前缀称作完整 SQL。

用户未指定时区时，“今天／昨天”默认按北京时间 `Asia/Shanghai`；本会话用户指定的时区优先，再换算到审计写入时区。精确条数、时间过滤与耗时排序由数据库执行，不先获取 100 条再摘要。`list_slow_queries` 没有慢耗时阈值，其 `max_rows` 默认 20、可配置至 100；分钟窗和逐条过滤的候选不能冒充全范围 Top N。直查失败保留原问题条件并如实说明，不静默改候选宣称成功。默认值和工具信息已经送达的离线验证不代表真实模型会正确选择；目标 StarRocks 的 `CURDATE()/NOW()` 与时区组合仍须真实目标验收。 普通查询返回的无时区 DATETIME 不自动补偏移；审计时间依审计写入时区解释，不能把连接时区当成所有列的写入时区。旧历史和已绑定文件可能保留原先错误附加的时区偏移；本次只纠正新查询，不追溯修正旧结果，也不批量失效历史；如需重新核对旧时间须明确要求刷新，排版追问不触发新取数。

- `database` / `table`：审计库表名（官方默认 `starrocks_audit_db__` / `starrocks_audit_tbl__`），只读账号需要该表的 SELECT 权限。
- `time_zone`：必须等于 FE 服务器的系统时区（审计时间按它写入）。配错不会报错，但时间窗会偏移，结果为空或错位；示例中的 `Asia/Shanghai` 部署时按实际核对。
- `stmt_limit`：必填，填插件的 `max_stmt_length`，并要求 `policy.max_sql_bytes <= stmt_limit - 4`，保证读出的原文没有被插件截断。
- `max_window_minutes` / `max_rows` / `candidate_rows` / `candidate_bytes`：可查的最长时间窗、最多列出的行数，以及一次取出并检查的候选行数与字节上限。

没有任何集群配置 `audit` 时不提供 `list_slow_queries`，配置开放或授予它会在启动时报错；只给部分集群配置时，只有这些集群可以使用它，其他集群的调用在连接前拒绝。读取该集群所有库的审计记录，`database` 参数不为 null 时只读执行时当前库为它的记录。原文中未写库名的表按该记录执行时的当前库（结果中的 `database` 列）解析，与 StarRocks 执行时相同；当时没有当前库的记录只接受写明库名的原文。只列出原文引用的对象、列与函数全部获准的查询，列表可能少于上限，为空也不代表没有慢查询；结果给出 `checked_candidates`、各 `omitted_*` 原因和 `unchecked_candidates`，`row_count` 仍仅表示实际返回行数。计数只覆盖本次已读取候选，`more_candidates=true` 表示候选读取已截断、之外数量未知；数据库侧 `isQuery` 与 catalog 过滤的条数也未知，需要完整审计口径时直接查询获准审计表。结果标记截断表示列表不完整：候选读满 `candidate_rows` 仍有剩余、候选达到字节上限、已列满 `max_rows` 而还有未检查的候选（即使它们最终可能都不获准），或获准的记录因单值或结果字节上限没有列出。插件刚安装后首批记录可能缺失；审计表任一行格式异常（如时间为空、指标不是整数）时整个工具失败，而不是返回部分结果。候选读取与原文检查各有一个客户端期限，最坏总耗时约为 2 倍 `client_timeout_seconds`。审计证据字段变更会使旧慢查询证据失效，无需数据库迁移。

**开发验证**（只用合成数据、脚本模型、替身与隔离的测试 PostgreSQL；发行测试需要本机 Docker 构建镜像，不调用真实模型或公司服务）：

```bash
uv sync --locked --extra dev
docker compose -p xiaowei-sdk-test -f compose.sdk-test.yml up -d --wait   # 或独立的 docker-compose
SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres \
  uv run --locked --extra dev python -m pytest -q
uv run --locked --extra dev ruff check .
uv run --locked --extra dev mypy src
docker compose -p xiaowei-sdk-test -f compose.sdk-test.yml down -v
```

`SDK_TEST_POSTGRES_URL` 只能是上面这一个测试地址；未设置时数据库用例明确失败而不是跳过。CI 的 integration job 以同样方式运行完整测试。

自主调查与普通消息沿[同一 T1–T5 样例](docs/superpowers/plans/2026-10-20-agent-autonomy-progress.md)验收。已有交互评测脚本增加 `autonomy` 集：T1/普通消息订单统计、T2 慢查询候选和 T4 缺环境，复用正式 runtime、治理及合成数据；T3 部分监控源失败复用 `test_monitoring_read_recovery.py` 的正式入口。离线夹具的基础订单聚合使用内存 SQLite，不能证明 StarRocks 方言或优化器行为。获得模型 Profile 和费用授权后，在交互终端设置 `XW_EVAL_PROFILE` 为其文件绝对路径，再运行以下命令；本轮没有可用真实模型，尚未执行此评测：

```bash
uv run --locked --extra dev python -m scripts.dialogue_real_model --profile "$XW_EVAL_PROFILE" --suite autonomy --repeats 3
```

脚本记录选择、I/O、耗时和可取得的用量，逐项人工核对结论、取证、澄清及限制；不以固定调用顺序或脚本模型通过代替 Agent 能力验收。SQLite 不支持的合法 StarRocks 语法（例如带 INTERVAL 的 DATE_SUB）记为“替身不支持”，不能计为 Agent 失败或要求模型改用固定 SQL；在实际 StarRocks 上复核。T3 及实际飞书效果仍在获准的目标环境按同一业务样例验收。

回答修复计划 P4 复用上述采集器的 `p4` 集，加入原始六轮对话、精确查询失败/仅候选能力、去重/用户统计、时区覆盖、监控双源局部失败、本轮与历史 brief 混合及 MD/HTML 交付。用真实 SDK、正式 runtime、隔离 PostgreSQL、loopback MCP 和现有分项绑定/回执路径，外部模型与渠道 I/O 在离线测试中使用替身。相关离线和 Chrome 检查：

```bash
SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres \
  uv run --locked --extra dev python -m pytest tests/sdk_core/test_answer_repair_eval.py tests/sdk_core/test_dialogue_real_model.py tests/sdk_core/test_autonomy_tasks.py tests/sdk_core/test_dialogue_contract.py -q
SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres \
SDK_TEST_CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  uv run --locked --extra dev python -m pytest tests/sdk_core/test_answer_repair_eval.py -m browser -q
```

另行取得具体 Profile、端点/模型和费用授权后，才能运行真实模型采集；当前未运行：

```bash
uv run --locked --extra dev python -m scripts.dialogue_real_model --profile "$XW_EVAL_PROFILE" --suite p4 --repeats 3
```

基线/候选须使用相同 harness 和输入，分别至少三次；不得给旧源码额外注入新时区或 view 指引。标准输出报告仅含版本、固定计数、用量/耗时与人工判定；实际 SQL/PromQL、正文和文件只在交互终端即时核对，不存入该报告。`first_body_ack_ms` 是已绑定正文的合成回执时刻，`complete_delivery_ms` 是全部分项回执确认时刻，两者都不能证明真实客户端可见。loopback MCP 只支持有限的合成表达式；未支持表达式与 SQLite 执行错误单独计数，交由人工在真实目标复核，不自动判为 Agent 失败。空人工结论不会被计作通过。真实模型自主选择、StarRocks 日期/时区语义、飞书桌面/手机多附件与用户验收仍开放。

P3-A 与 P3-B 已经独立审查并合入；精简发行包、两容器 Compose、配置预检、首次初始化与停止步骤见 [deploy/OPERATIONS.md](deploy/OPERATIONS.md)。小维连接已有 StarRocks 和模型 API，不要求在服务器部署模型；应用配置、`.env`、CA 与 PostgreSQL 命名卷由操作者保留。amd64 发行包自带应用镜像文件（`docker load` 导入，不需要镜像仓库）；公司服务器与真实服务验收仍待 P3-C。正式镜像使用 `Dockerfile.runtime`，部署使用 `deploy/compose.yaml`；根目录 `compose.sdk-test.yml` 只供隔离测试。

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

当前产品源码仅保留 `src/xiaowei`。旧 `xiaowei_agent`、专属测试、Compose/Worker/迁移入口及依赖已移除，历史可从 Git 查回；新产品仍保留旧回答和存储版本的兼容路径。旧 ADR 和里程碑仅供历史参考，不能与上述设计共同作为新产品规范。
