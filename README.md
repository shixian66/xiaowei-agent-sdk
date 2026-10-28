# 小维 · 受治理运维助手

小维基于 **OpenAI Agents SDK**，在 Web、飞书获准单聊与一个指定群中完成 StarRocks／TiDB 查询诊断、监控调查和 DolphinScheduler 只读排障。获准用户还可使用通知、定时只读任务、历史追溯及逐次审批的选中监控写动作。

> 下文说明当前主线的能力与实际用法；代码已合入不等于每个模型、公司数据源和真实渠道均已验收。精确版本、证据与开放项统一见 [AGENT_HANDOFF.md](AGENT_HANDOFF.md)。

**OpenAI Agents SDK 负责 Agent Loop；小维负责权限、受治理工具执行、证据真实性和数据边界。**

## 当前能力

- 发现获准 StarRocks／TiDB 的库表、字段与结构，执行有界只读 SQL 并解释结果。
- 用 SQL 计划、审计／诊断表和监控事实调查慢查询、锁等待及集群问题。
- 按自然对话选择 DS 环境，定位失败实例／任务，在长日志中搜索错误并补读上下文。
- 连续追问或按权限找回历史事实，交付自然业务回答、表格及飞书 MD／HTML 报告。
- 在独立授权下发送机器人通知、批准定时只读任务，或逐次审批选中的监控写动作。

这些能力共用一个业务 Agent。数据库走本地 Adapter，DS 和 Alertmanager 走只读或已批准的 API 路径，Prometheus／Grafana 通过 SDK MCP 接入；所有工具在 I/O 前复核权限、目标与预算，结果经过独立投影和 Evidence 验证。数据库 MCP 暂缓。

多集群、自动数据范围、复杂 SQL、普通消息自然选择和指定群已在主线。数据库与 DS 的环境沿用规则不同，见下文；不要用旧 P2 单目标配置或“待开发”状态判断当前能力。长期边界见 [ARCHITECTURE](ARCHITECTURE.md)，开发阶段和退出条件见 [DEVELOPMENT_PLAN](DEVELOPMENT_PLAN.md)。

**监控能力（P4）。** 同一 Agent 可调查获准的 Prometheus 指标/标签/规则和范围结果、Grafana 仪表盘/面板/数据源/标记，以及独立 Alertmanager 的告警/分组/静默/接收器/状态；Grafana 定义与实时指标分开，不执行 Grafana 代理查询。指定群可选配逐次批准的静默创建/取消、经典 Dashboard 创建/修改、数据源非秘密字段创建/修改和 Annotation 创建，使用 SDK 原生暂停/恢复；这些实现不代表公司生产写已开放。数据源密码目前由管理员在 Grafana UI 配置，私聊密码命令尚未实现。主配置默认不启用监控源；选配、认证、迁移与开放门槛见 [Prometheus](deploy/OPERATIONS.md#选配-prometheus-监控源)、[Grafana](deploy/OPERATIONS.md#选配-grafana-监控源)、[Alertmanager](deploy/OPERATIONS.md#选配-alertmanager-监控源)、[Dashboard 写](deploy/OPERATIONS.md#选配经典-dashboard-创建和修改)和[群审批说明](deploy/OPERATIONS.md#监控群审批底座)。产品边界见 [ARCHITECTURE](ARCHITECTURE.md#monitoring-mcp)，合入版本、验证证据与缺口集中在 [handoff](AGENT_HANDOFF.md)。

通知 A、固定提醒 B、定时 Agent C 和自建代理适配已在主线，配置见[Webhook 通知](#webhook-notifications)及后续定时章节。默认关闭，真实模型、代理协议／群实收和实际并发成本仍按 [A–D 计划](docs/superpowers/plans/2026-10-21-scheduled-agent-tasks.md)验收；Node 专项 D 暂停、未实现。CloudCanal 当前只有[独立 CC0 探针](#cloudcanal-cc0)，尚未接入产品。

## 产品形态

| 项目 | 当前实现与配置 |
| --- | --- |
| Agent 核心 | OpenAI Agents SDK 的 Agent、Runner、function tools、Session |
| 模型 API | OpenAI／Gemini／DeepSeek 及兼容端点的 SDK 路径、Vertex 原生 Model 薄适配；一份配置一个活动 Profile，具体端点／模型分别验收 |
| Web | 简单对话页：原生启动默认只监听本机（`listen_host` 可改）；Compose 部署默认发布到宿主机所有地址（`XW_WEB_BIND_ADDRESS=0.0.0.0`），供公司内网访问；无登录，访问者共用一个操作者身份 |
| 飞书 | 获准单聊；可选一个指定群：群内 @小维 提问、全员共享会话、同群有界排队并回复原消息（已合入，真实平台各项验收见 handoff） |
| 数据源 | `targets` 配置一个或多个 StarRocks／TiDB，`cluster` 选择目标；另可配置 DS、监控源。业务对象按真实账号权限自动发现；诊断源按各自契约 |
| MCP | 官方 SDK 接入能力 + 小维可信配置与治理；配置为空时，本地功能照常运行 |
| 会话 | 两端分别保留上下文，共用业务逻辑；暂不跨渠道同步 |
| 运行与存储 | Docker Compose 管理小维与 PostgreSQL 两个容器；小维单进程，SDK Session 首版使用 SQLAlchemySession + PostgreSQL；飞书优先长连接 |

会话继续使用 SDK 公共 Session 接口；SDK 历史与应用记录各自管理表结构，共用 PostgreSQL 实例。数据保存在持久卷，并提供备份恢复办法。引入数据库不增加 Worker 或分布式调度。当前显式关闭 tracing 与 trace 外发，没有配置开启入口；将来开启须先落实独立授权、字段／接收端和期限约束。模型 API 仍会接收获准输入。

使用方式：

- 普通消息（Web 用途选“默认”，飞书直接发文字）由小维按本条消息判断：明确要求查数据，且目标与必要口径清楚时执行。需要时间条件时使用可信上下文中的业务时区和口径，缺失且影响答案才问；查结构、列库表或无时间条件的统计不必追问时间。准确对象与列已有可信信息时直接使用，缺哪项补哪项，不强制“搜表→查列→查询”。原文请求不主动截短，实际容量超限须说明；翻页按游标继续，与截断重查区分。只要求解释、只写 SQL、说了“不要执行”、只贴一段 SQL 或缺少关键条件时，只给解释、建议或澄清；有证据仍需用户选择时展示已核实部分并在分析中提问。回答须完成用户要的结果或诊断并说明依据；由 Agent 选择所需业务事实，不默认附送全部中间取证结果。建议或澄清使用的本轮及历史数据同样复核当前权限，隐藏原始结果不绕过授权。上轮查过数据、引用别人的话或表注释里的文字都不代表本轮要求执行。这是 AI 的判断，不是确定性的安全关卡：误判时查询仍受只读账号、SQL 检查、当前权限与资源限额约束，但通过这些检查的只读 SQL 会被执行。历史回答不证明消息已送达或已读；重新展示历史保留采集时间，用户明确要最新结果才重新查询。
- 想确保不执行数据库业务 SQL 时，Web 选“仅诊断”或飞书发 `/诊断 内容`：本条消息看不到也不能调用数据库业务查询工具；结构、计划、已登记诊断取证和 DS 只读能力仍按权限可用，监控工具不在显式诊断用途内。飞书 `/查询 内容` 与普通消息相同。API 调用仍须显式给出用途（`query` 即默认用途，`diagnose` 即仅诊断）。
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

StarRocks／TiDB 和 DS 只读能力不执行数据修改、配置变更或优化建议；监控写仅限上文 P4 已批准的动作；不建设复杂管理后台、多 Agent 或分布式任务平台。

已接入的监控写按具体 Action 管理；其他数据库修改、配置调整、取消查询、重启与发布仍须单独批准范围。所有生产写操作都先展示目标、改动和影响，取得有审批权限的用户明确确认，再复核权限后执行；目标或关键参数变化需要重新确认。“帮我诊断/优化”不代表同意执行生产变更。

**运维历史（H1/H2已合入）。** 新建后可通过 `history/search_operations` 与 `history/read_operation` 找回当前获准的原始事实和调查结论；生效范围只见[运维历史契约](ARCHITECTURE.md#operations-history)。工具仍取现有用途、模型白名单及当前读者授权的交集，不自动增加配置许可。搜索按带时区的绝对窗口、目标、已知任务/运行筛选；固定最多10条、扫描20个候选；不复制分析的原事实路径每条最多10份原Evidence。详情复制旧分析时完整复核包含会话回放的依赖，不受10份计数限制，仍受去重对象数、字节与预算约束。因容量或对象上限未复制分析只标截断，原回答不存在或无法解析才标回答缺失。更早候选仅针对本次时间窗，不保证内容可读，也不代表窗口起点前没有记录；未找到不证明未发生；可缩小或前移窗口、按已知编号直读，或用获准原源补证。旧分析与原事实分列，来源失效时不交付依赖它的分析；历史状态不证明当前状态，旧批准不授予新写权限。

H2将校验通过的定时调查结论、证据引用和未完成状态保存为有界快照；仅有文字时也在提交与发送前复核任务冻结原源，期限不超过原数据及请求保留期。通知拒收或发送失败不抹掉已保存结果。新会话在创建任务的操作群中仍按当前读者与冻结原源复核后读取。定时Agent自身目前只检索同操作群的定时运行结果，不能通过历史工具读取群聊交互调查；用户2026-10-27已选择保留此范围，见[ARCHITECTURE B1](ARCHITECTURE.md#scheduled-interactive-history-choice)，D1不变。升级前没有快照的运行只返回有效原事实和缺口，不从旧SDK消息重建；到期清理正文但保留运行元数据。真实模型自主性、公司源、真实渠道实收和因果判断留待 H3/公司验收，当前版本的公司验收缺口见 handoff。离线接线与边界检查使用下方开发验证的隔离 PostgreSQL，再执行：

```bash
SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres uv run --locked --extra dev python -m pytest tests/sdk_core/test_operations_history.py tests/sdk_core/test_operations_history_tasks.py tests/sdk_core/test_scheduled_history_snapshot.py -q
```

## 怎样开始

唯一正式命令是 `xiaowei`（`python -m xiaowei` 相同）。启动、存储维护与正式入口已通过隔离 PostgreSQL、脚本模型／替身和本机容器验证；已有公司部署与使用记录，完整业务验收仍按版本／模型／数据源／渠道分别判断，见 handoff。amd64 包由手动发行流程生成并携带离线应用镜像；旧 CLI、Worker 或旧 Compose 不是产品入口。

运行前准备：

1. 开发使用 Python 3.11 与 uv，部署使用 Docker 和 Docker Compose；新依赖和镜像版本验证后锁定。P1-A 的存储验证也使用隔离的真实 PostgreSQL。
2. 一家获准的模型服务：模板默认 Vertex AI（API Key 模式，只需模型 ID 和 Key，地址与协议由程序固定）；也可用 OpenAI、Gemini、DeepSeek 或兼容网关，此时须明确 API 地址、协议和模型 ID。Key 都只写本机安全引用。
3. 至少一个获准的 StarRocks／TiDB 目标、真实只读账号及业务口径；按需增加 DS／监控源，并确认模型与渠道可接收的数据范围。当前 ServeConfig 不支持 `targets=[]` 的纯 DS／纯监控部署。
4. 飞书企业自建机器人、消息权限、事件订阅及获准单聊用户；启用指定群时另需机器人入群，以及群内 @ 消息事件、获取群成员与回复消息的权限（见[单群计划 F0 证据 7](docs/superpowers/plans/2026-10-03-feishu-group.md)）。

凭据仅在本机或部署环境配置，不粘贴到对话、仓库、浏览器或日志。缺少真实环境时可以开发和离线验证，但不能标记对应实战验收完成。

**正式命令。** 服务器首次安装按 [运维说明的“首次安装”](deploy/OPERATIONS.md#首次安装)：复制两份模板、填写环境变量并核对目标，再依次 `config check` → `model check` → 初始化 → 启动。原生运行同样从 [examples/xiaowei.example.json](examples/xiaowei.example.json) 开始：它含用户批准的公司 `fat` 集群与 Vertex 模型非密钥配置；在其他环境使用时必须先改目标、模型和 TLS 设置。改用其他供应商时 `model` 段另填 `base_url`、`api_mode` 与 `output_mode`。凭据只写 `env:NAME` 引用，变量在运行环境中设置：

```bash
uv sync --locked
export XW_DATABASE_URL=...   # postgresql+asyncpg://...，以及 XW_DIGEST_KEY、XW_MODEL_API_KEY 和各数据库／外部源的凭据环境变量（主模板只有 XW_STARROCKS_PASSWORD）
uv run --locked xiaowei --config xiaowei.json config check      # 离线核对配置、环境引用和本地 CA
uv run --locked xiaowei --config xiaowei.json model check       # 只需模型 Key；向模型发一次合成工具往返，产生用量
uv run --locked xiaowei --config xiaowei.json storage init      # 全新数据库
uv run --locked xiaowei --config xiaowei.json storage upgrade --bind-existing-digest-key  # v1–v5；仅确认原密钥并备份后
uv run --locked xiaowei --config xiaowei.json serve             # Web 默认 http://127.0.0.1:8501，Ctrl-C 停止
uv run --locked xiaowei --config xiaowei.json storage cleanup --batch-size 100
uv run --locked xiaowei --config xiaowei.json requests resend --subject <subject> --chat <chat_id> --message <message_id>
uv run --locked xiaowei --config xiaowei.json requests resend --subject <发起人 open_id> --group --message <message_id>
```

`serve` 持有数据库实例锁并在启动时执行中断恢复，第二个实例会被拒绝；普通启动不建表、不升级。`requests resend` 只重发飞书中投递为 failed/unknown 的已保存结果，不重跑模型或查询；发送前按当前权限和各源契约复核依赖；数据库对象可能进行零行权限／元数据探测，TiDB 视图仍走受保护路径。因此需要相应安全引用、网络和权限，重发不等于完全没有外部 I/O。目标暂时连不上时不发送、记录保持可再次重发；确定撤权或对象已变化时拒绝。退出码：0 成功，1 运行失败或请求被拒，2 参数或配置错误。Web 只提供 HTTP；`web.allowed_origins` 为旧配置兼容字段，当前不限制 Host/Origin，访问范围由部署网络控制。持锁的数据库连接一旦断开，进程立即停止接收并以 1 退出；新实例接管时会等待旧实例仍在提交的请求接收，再执行恢复。正式日志只输出小维自身日志（级别由 `--log-level` 决定），依赖库的日志在任何级别都不输出。

飞书配置：主模板的 `feishu` 为 `null`；启用时从下方飞书段示例复制配置。`feishu.domain` 只接受 `"feishu"` 和 `"lark"`：应用后台网址是 `open.feishu.cn` 时选 `"feishu"`，是 `open.larksuite.com` 时选 `"lark"`；省略时默认 `"feishu"`。

**飞书指定群（可选）。** 含指定群的完整 `feishu` 段见 [examples/feishu-group.example.json](examples/feishu-group.example.json)，放进主配置的 `feishu` 键后按获准环境替换占位值；示例的单聊名单 `users` 为空：此时单聊没有人获得权限，未登记的人私聊机器人会收到一条只含其本人 `open_id` 的固定提示，用来交给管理员登记；登记单聊用户时须同时在 `access.grants` 给其 subject 非空工具，否则 `config check` 与启动都会拒绝（见[运维说明的“启用飞书”](deploy/OPERATIONS.md#启用飞书)）。群成员不需要出现在 `users` 中。在 `feishu` 段加入 `group`：`chat_id`（`oc_` 开头）、`tools`（全员同权共享的提出/读取工具，只能是已登记工具；实际写工具不放这里）、`member_page_size`（≤100）、`member_max_pages`（≤20）、`member_timeout_seconds`（≤30），以及同群排队的 `max_waiting`（等待条数上限，只不计正在运行的一条，已轮到但还在等处理能力的一条也计入；≤100 且不超过 `queue_size`）、`max_wait_seconds`（从接受起的最长等待，须短于 `storage.request_retention_seconds`）与 `wait_check_seconds`（到期检查间隔，≤60 且不超过 `max_wait_seconds`）。启用指定群时 `feishu.stop_timeout_seconds` 不得小于 7（飞书 SDK 关闭时可能固定等待约 6 s）。只处理这个群里 @小维 的文字消息：按平台 mention 中机器人的 `open_id` 识别（启动时由 SDK 取得，取不到时群消息一律不处理）；只写“@小维”文字、@其他机器人、其他群或机器人发的消息都不处理。群成员不需要出现在单聊 `users` 中，在群里能用也不代表能用单聊或 Web。群内成员共享一个会话：B 可以追问 A 刚才的结果，`/新建` 对全群生效；交给模型的每条消息首行带一个由发送者算出的短标识（不含 `open_id`），用于区分谁问的，不赋予任何权限。每轮开始运行前和每次发送前都经飞书接口重新确认发起人仍在群里；已取到的名单（群规模超过页数上限时只是部分名单）中找不到发起人、接口失败或超时都按无法确认处理，本轮不执行或不发送，也不发提示；群规模较大时需相应调高页数上限。回复一律回复原消息；原消息已撤回或不可见时记为发送失败，不会改发新消息或发到别处。发出的文字中的 `<at …>` 改为全角，回答不能 @ 任何人。群结果重发用 `--group` 并给出原发起人的 `open_id`，只回复原消息，发送前同样重新确认成员身份。同群同一时刻只运行一轮：其余提问按小维接受的顺序排队（不按飞书消息时间），新排队的提问会收到一次“已收到，前面还有问题在处理，将按顺序回复”，前一轮的结果保存并发送一次后才开始下一轮，下一轮能看到上一轮的结果；排队不占用单聊或 Web 的处理能力。排队提示的发送结束之前，小维不会开始运行这条提问，也不会发出它的繁忙回复；这只保证小维这边的发送先后，飞书上显示的先后仍受网络、发送期限和结果不明的情况影响。等待提示发送时不占用单聊或 Web 的处理能力。排队已满或等待超过 `max_wait_seconds` 的提问记为“系统繁忙”并回复原消息，不会执行，可稍后重新发问；尚未开始运行的提问（包括已轮到、但还在等处理能力的下一条）都计入排队上限并按这个期限处理。`wait_check_seconds` 只决定多久发现并记下到期，回复何时到达还取决于发送前的成员确认、网络与发送期限，到期回复尚未发完的提问仍计入排队上限；这期间飞书重复投递同一提问不会再确认成员或重复回复。排队或运行中发 `/新建` 会被拒绝。重启时只在排队、尚未开始的提问记为中断，群会话保持可用；已开始运行的一轮被中断时，群会话照常关闭，需 `/新建`。飞书入站不再请求通讯录接口（单聊同样）。


**数据库多集群提问（StarRocks／TiDB）。** 先告诉小维用哪个环境，例如“用 fat”或“查看 fat 的 data_center.quota_virtual_account_quasi_rt 结构”；没说环境时小维会先问，不会自己去各环境查找。选定后同一会话的追问沿用该环境，说“切到 pre”才换；只是问“pre 上也有吗？”时小维会先确认是否切换。群里成员选定的环境全群沿用，但每轮权限和执行意图仍按发起人独立复核；`/新建` 后需要重新选择。只写或解释 SQL、不需要查库时不用选环境。具体边界见 [R7 目标规则](ARCHITECTURE.md#r7-targets)。这是提示与工具说明的改进，执行层不会拦截模型选错环境；真实模型的遵守情况尚未评测。

**诊断。** 普通消息可请求调查；Web“仅诊断”或飞书 `/诊断` 会隐藏数据库业务查询工具，仍可按授权读结构、计划、审计／登记诊断表、历史与 DS；监控工具在普通用途使用。StarRocks 只取 `EXPLAIN LOGICAL`，TiDB 使用目标版本的受保护计划路径，均不执行原查询或 EXPLAIN ANALYZE。可以问“刚才那条为什么慢”或“最近一小时最慢的查询”；缺审计／慢日志时须说明。结果附估算、导入延迟、截断等必要说明，建议 SQL 不自动执行。

飞书普通解释、问候、建议和澄清使用受控 Markdown；业务结论、所选表格及必要执行状态直接呈现。新 v4 回复至多一个默认收起的“技术详情”，不逐项附工具标题或依据入口；目标、数据时间、截断、失败与 unknown 保持可见。用户索要实际 SQL 或已保存原值时从当前获准事实交付，不重跑原任务。旧保存版本的显示和补全、已绑定原字节恢复仍保留。审批卡完整独立交付；暂停前的只读结果待获准恢复后由最终回答选择交付，卡片会说明尚未交付。JSON 2.0 卡片要求客户端至少 7.20，真实客户端仍待另行验收。契约及保存格式兼容见 [长期交付规则](ARCHITECTURE.md#user-delivery-and-history)。

飞书结果按用户需要和容量交付：短业务回答一条消息，未索要的技术内容不会单独触发附件。所选业务结果放不下时转业务 MD 文件加正文预览；明确要求 HTML/MD 仍复用同一 Agent 和获准事实，不为格式增加查询。显式与自动附件默认只交业务内容，保真业务原值和嵌套类型；技术元数据仅在正文技术区，放不下明确未附。自动附件保持历史摘要语义，细则见[长期交付规则](ARCHITECTURE.md#user-delivery-and-history)。单文件 400,000 字节及整行/章节拆分、失败和手动补发沿原机制。审批材料优先独立发送，既有已绑定参考数据按原字节恢复。使用及平台未覆盖项见 [报告文件说明](deploy/OPERATIONS.md#飞书报告文件)，代码/离线通过不等于部署或真实实收。

**自主调查与普通消息。** 连续模型段落使用轻量区域身份；本轮 brief 原位收起并完整交付，未引用结果按 v4 规则不自动补交，所需技术 metadata 合并到默认收起的技术区；历史 brief 保留计数摘要，未展示原文时明确提示，不自动导出历史原值。必要范围、数据源警告与截断仍可见。展开面板和附件保留获准内容，模型文字仍是分析或建议。代码草稿局部保持原文，其余段落继续排版。飞书进度在单聊约 2 秒、指定群约 5 秒后显示，只按真实状态变化尽力更新；短任务没有过程卡，异常可能留下旧卡，正式结果承担交付。真实 Agent、客户端折叠/复制、平台权限及限频由后续公司环境验收，详见[实施与验证](docs/superpowers/plans/2026-10-20-agent-autonomy-progress.md)。

**模型自主报告。** 显式报告由同一个 Agent 通过原有文字/事实块组织：MD 原生显示标题、列表和表格；HTML 支持独立片段和受限内联样式，分析与代码生成的可信事实就近排列。主消息提取可读预览，不发送布局源码；报告指南随包按飞书渠道加载，无 StarRocks 目标也可用。文字仍受 16,000 字节和模型输出 token 限制；脚本、外部资源和任意网页执行不开放。新回答按可信 v4 保存，v3 也支持原生报告，旧版本/已绑定内容保持原语义；模型 schema 不变；当前应用数据库版本见下方升级说明。v4 回退按[长期规则](ARCHITECTURE.md#user-delivery-and-history)前向修复，不恢复旧备份。离线 SDK/治理/存储及 Chrome 检查不能证明真实模型的报告质量、真实飞书实收或部署效果，见[实施计划](docs/superpowers/plans/2026-10-19-model-authored-reports.md)。

**监控曲线。** 暂不实现，监控结果以文本/表格交付。[计划 D4.0](docs/superpowers/plans/2026-10-14-result-delivery-documents.md#d40-协议前置结果与最小修订提案2026-10-15)保留已完成的协议核验、反例与候选路径对比，供以后明确重启时参考。

指定群的纯文字富文本 post 与 text 共用提问及 `/查询`、`/批准`、`/新建` 入口，仍要求同一条消息的平台 @本机器人及当前成员资格；仅有正文 at 标签不授予入口资格，私聊 post 尚未开放。图片和文件默认关闭；可显式开启 `feishu.document_input_enabled` 接收 UTF-8 TXT/MD，截图还须同时开启 `feishu.image_input_enabled` 和 Vertex Profile 的 `image_input_enabled`。独立图片或一图 post 只接受单张不超过 2 MiB 的 PNG/JPEG，每会话最多 12 张，保留同条问题并在后续提问回放原图。带图那一轮只作诊断、不执行数据库业务 SQL，图文中的命令不执行；多图、嵌入媒体/附件或非法结构整条说明未处理，不截短或只取第一图。post/mentions 的真实平台形态、模型识图、客户端和 12 图请求尚未验收；配置、容量和回退见[运维说明](deploy/OPERATIONS.md#飞书-pngjpeg-输入)。

**数据库目标（`targets`）。** 数组至少一项，`type` 支持 `starrocks` 和 `tidb`，连接配置分别在同名键下；集群 ID 来自 `starrocks.target_id`／`tidb.target_id`，1–32 位小写字母、数字、`_`、`-`，以字母或数字开头，不能重复。`description` 是交给模型的环境说明，不代表默认或已选；`business_context` 为 `{"version", "text"}` 或 null，说明业务口径，不扩大权限。连接地址、账号和凭据引用不交模型。授权按现有工具规则授予，获准用户可访问配置目标；调用必须指定 `cluster`，未知目标在 I/O 前拒绝，不换源。新增／删除目标或修改口径会要求旧会话新建；仅改用途说明不改变会话绑定。旧顶层 `starrocks`／`business_context` 配置须迁到数组项，不能双读。StarRocks 用主模板，TiDB 用下方目标示例；方言和对象复核不能混用。

**DolphinScheduler 只读 API（已合入，待公司验收）。** 可选 `dolphinscheduler_sources` 缺省为空，六项业务工具与一个本地选择工具复用同一 Agent、用户／指定群授权和四种数据投影。把 [单源示例](examples/dolphinscheduler-source.example.json) 作为数组一项加入配置；3–6 套分别配置唯一 `cluster_id`、用户认得的 `description`、真实 `version`（3.1.8／3.4.0）、API 根路径、IANA `time_zone` 和独立 `token_ref` 环境变量引用。ID 不与其他目标重名。源绑定、运维参数调整及历史读取规则见 [DS 架构](ARCHITECTURE.md#dolphinscheduler-environment)。响应上限固定为元数据 1 MiB、单日志 16 MiB，不配置 max_response_bytes／max_log_bytes；timeout_seconds 可选，默认 15 秒。推荐 HTTPS；HTTP 的明文传输边界见 [DS 架构](ARCHITECTURE.md#dolphinscheduler-environment)。固定根地址不可带账号或 query，Token 不写进配置／聊天；API 账号可见项目与日志权限须在公司核实，不能假定 Token 天然只读。

在 `data_policy.model_tools` 和相应 `access.grants`（指定群则 `feishu.group.tools`）按需加入 `local/ds_select_clusters`、`local/ds_list_projects`、`local/ds_list_workflow_instances`、`local/ds_get_workflow_instance`、`local/ds_list_task_instances`、`local/ds_read_task_log`、`local/ds_search_task_log`；沿既有授权方式可访问配置源，不另建 DS ACL。保留现有预算，诊断较复杂时按成本配置，例如单轮 16 次工具、12 次模型轮；不增加新循环。`automatic_tools` 本期保持不含 DS。用现有 `config check` 离线核对，配置检查不探测 DS、不会证明账号权限或模型能力；随后显式升级应用表至 17 才启动。现有实现证据使用合成 DS／脚本模型，示例不是公司已验收配置。

用户说“查 DS pre 昨晚失败任务”即可；未说或歧义时先自然澄清，未选不读取。比较 A／B 后“今天呢”沿用两套，“B 上也这样吗”切到 B，仅提及不切换；新会话清空选择。详情、任务历史与日志片段可按证据自主组合，不要求固定顺序。日志读／搜共享单轮一次下载：单份最多 16 MiB、整轮跨源最多 32 MiB，单次片段投影至多 16 KiB；行号去除下载日志头，超长行用 UTF-8 字节偏移补读。覆盖说明区分 EOF 与截断，并报告实际遮蔽字节数；未找到不等于不存在。认证／建连失败停该源本轮，日志对象超时只停该对象，不自动重试。真实 Agent 的选择、诊断质量和服务端大日志负载仍须按 [固定验收](docs/superpowers/plans/2026-10-28-dolphinscheduler-readonly-api.md#91-固定业务样例) 验证。

**StarRocks SQL 与历史兼容。** 连接固定 SQL 模式，`||` 表示逻辑 OR；完整契约见 [ARCHITECTURE §6](ARCHITECTURE.md#6-只读查询保护)。当前 StarRocks 数据范围摘要为 `/8`，与应用 schema17 不同；历史摘要或查询策略失配的证据不可回放／重发，不能把旧结果当新取数。交付复核回答中全部模型可见 Evidence，隐藏详情或只留文字不绕过权限。具体旧版本变化可从 Git／计划追溯，升级按当前配置、存储及绑定检查处理。

**应用表版本17、升级与恢复。** 版本 5 保存个人/群 owner 与群回复目的地；版本 6 把数据库与 `XW_DIGEST_KEY` 的不可逆指纹绑定；版本 7 新增监控 Action 表；v8 新增请求会话状态，v9 新增飞书会话原图表，v10 保存 SDK 暂停/恢复检查点与专用 Action 作废原因，v11 新增原请求绑定的飞书报告文件表，v12 新增最小交付分项与父请求语义版本，v13 新增通知发送机会/回执，v14 新增一次性提醒，v15 新增独立 Agent 逐次运行及通知关联，v16新增有界定时结果快照与期限，v17仅新增 DS 已提交环境集合，SDK历史表不变。v8/v9 的旧待批 Action 升级后作废、需重新提出，成功/未知事实保留。`serve`、升级、清理和重发遇到错密钥都会在业务 I/O 前拒绝。v1–v5 只有在操作者确认 `.env` 仍是原部署密钥并已完成配套备份后，才可执行 `storage upgrade --bind-existing-digest-key`；程序不能替操作者证明旧密钥来源。v6–v16 升到 v17 使用普通 `storage upgrade`，必须保留原密钥绑定，缺失或错配不会自动修补。普通启动不建表、不迁移，旧程序也拒绝新 schema。历史跨 schema 隔离恢复不构成当前 v4 的回退方案；当前保存格式兼容与前向修复以[长期规则](ARCHITECTURE.md#user-delivery-and-history)为准，不在原库降级。容器首次安装、显式升级、原子 `pg_dump`、隔离 `pg_restore`、主机重启后的启动和排障命令统一见 [容器运维说明](deploy/OPERATIONS.md)。运行镜像与发行归档不包含根文档、源码测试或开发工具。

以下数据库工具细节主要描述 **StarRocks**；TiDB 的默认库、大小写、视图探测及诊断表差异见 [TiDB 查询与诊断](#tidb-usage)。

**StarRocks 数据范围（自动发现）。** 不再配置表与列：小维每隔 `schema_limits.refresh_seconds` 读取一次 `information_schema` 中全部用户库的表、视图与列，并对每个对象做一次不返回数据的 `SELECT 1 … WHERE 1 = 0` 探测，只有只读账号确实能 SELECT 的对象才进入范围。查询与执行计划可引用其中任何库的对象并跨库 JOIN：工具 SQL 中的物理表必须写成 `库名.表名`，不使用连接默认库或唯一匹配隐式补全。用户可以在提问中简写；工具收到未限定表名时先拒绝并返回有限候选，唯一候选可由 Agent 补库名后重试并告知用户，多个候选须澄清。`list_tables`、`describe_table`、`describe_table_layout` 在返回前会再次确认对象仍可读。

**列库（`list_databases`）。** 分页列出当前账号有可查询表或视图的数据库，只返回库名；不含空库、系统库或没有可读对象的库。每库确认至少一个对象当前可读，交付与历史读取仍复核该对象权限。每页最多探测 32 个候选对象；撤权库对象较多时可能出现空页但 `next_cursor` 非空，须继续取到 null 才算列完。页大小为 1 到 `policy.max_rows`。新工具需同时加入 `data_policy.model_tools` 和对应 `access.grants`（群还需加入 `feishu.group.tools`），现有部署不会自动获得该权限；示例配置已列出。

**搜表与列某库全部表（`list_tables`）。** 有关键词时，不区分大小写匹配“库名.表名”、表注释（按 `max_comment_chars` 截取）与列名，`database=null` 搜索全部库；要列出指定库的所有表，传 `database` 和 `keyword=null`。按库表名排序分页，`page_size` 为 1 到 `policy.max_rows`，结果字节上限可能令一页少于它；每页只确认本页对象的当前权限，撤权对象不列出。第一页 `cursor=null`，之后原样回传 `next_cursor` 并保持其他参数不变；取到 null 才列完，预算不够时须说明仍有未列出的结果。目录内容不变的定时刷新可以继续翻页；内容或参数改变后旧游标在 I/O 前拒绝。单个对象（含表注释）超过容量时拒绝，不能静默跳过。

**表结构（`describe_table`）。** 返回字段、完整类型（保留长度与精度）、是否可空和注释。列多时通过 `next_cursor` 分次取得，每次重验权限，目录内容改变后旧游标失效；单列超过容量时拒绝。主键与排序键须另查布局，不能凭字段名猜测；布局不等于原始 SHOW CREATE TABLE。桶数为 0 只报告元数据原值，不据此确认实际桶数或自动扩缩容。

**内部表原始 DDL（`show_create_table`）。** 直接返回普通内部表的 SHOW CREATE TABLE 原文，支持本地 `OLAP` 与存算分离 `CLOUD_NATIVE` 引擎，保留默认值、完整键、分区定义与表属性，不执行 CREATE。读取前后核对对象 ID、类型、引擎；记录、发送和历史读取仍复核当前 SELECT 权限、对象 ID 及采集时列的类型。视图、物化视图、外表和外部 catalog 尚未开放。服务器可能规范化语句并补默认属性，因此原文不保证等于最初输入；分区定义也不等于当前分区状态列表。历史 DDL 是采集时的定义：新增列、表属性或分区变化不会自动使旧原文失效，旧原文会带采集时间显示；要看现状请重新查询。

该工具需独立加入 `data_policy.model_tools` 与使用者的 `access.grants`，已有授权不会自动扩大。示例仅给 Web 使用者配置此能力，飞书群示例未加入。每个目标可配置 `starrocks.max_ddl_bytes`（JSON 编码字节数），未配置时沿用 `max_value_bytes`；仍须满足 `max_result_bytes` 与四种投影容量。示例 DDL 上限 64000 不是所有表通用的值；显式日分区很多的表可能超过，升级前须用目标集群的代表性 SHOW CREATE 原文测量 JSON 编码字节数，并核对结果及四种投影容量。普通查询单值上限示例为 4000 字节，超限值带截短标记并继续返回放得下的行；DDL 超限则整条原文不返回，不裁成半段后声称完整。

**Web 结果展示。**Web 主回复中的事实表格只显示一次，SQL、游标、来源与本次获准结果原文可展开；来源、采集时间、技术参数和完整原值在详情核对，正文保留一次 AI 说明及必要限制。澄清和未执行的 SQL 建议保留原换行。表格保留未截短值的大整数精度，区分真正的 NULL、字符串 `"NULL"`、空字符串与未提供字段；嵌套字段完整保留。原文仅包含本次获准展示的数据；超长单值只保留前缀与截短标记，无法从展示恢复原值或原类型，分页或截断也不等于已取全。“已回复”表示已交付，不代表每项业务需求均已满足。每轮向模型提供当前 UTC 时间，相对日期仍须结合明确的业务时区。

**SQL 写法（`run_readonly_query` / `explain_query`）。** 单条只读 SELECT 或 UNION/UNION ALL，可带非递归 WITH、子查询（含相关子查询）与窗口函数（PARTITION BY / ORDER BY，不支持窗口框架与命名窗口），函数须在 `allowed_functions` 中（窗口函数如 `ROW_NUMBER`、`RANK` 同样要列出）；部分函数接受常用写法与解析后名称，如 `DATE_FORMAT` 或同时列出 `TIME_TO_STR`、`TS_OR_DS_TO_TIMESTAMP`，`IFNULL` 仍需 `COALESCE`，详见[运维说明](deploy/OPERATIONS.md#常用上限与函数名单)。物理表须写成 `库.表`；缺库名时工具返回尚未过期的可读结构快照中的有限候选，实际执行仍复核当前权限，一个库可补全后重试，多个库须先澄清。`list_slow_queries` 的 SQL 取计划时应按该行 `database` 补库名。SQLGuard 只检查原文，实际执行不按解析器重打印；查询唯一可能的文字编辑是去尾分号、追加或下调顶层 LIMIT，事实区展示实际 SQL、原 LIMIT 与执行 LIMIT。带格式的 `FROM_UNIXTIME` 可以按函数授权执行；注释须删除后重试，hint 仍拒绝。`*` 与 `别名.*` 不改写执行 SQL，但按快照展开来检查引用和列数；原文与预计列头受 `max_sql_bytes` 限制，查询结果最多 `max_result_columns` 列，顶层及实际返回列名不能重复，无别名表达式可用 StarRocks 的返回列名。列名不区分大小写，库名、表名与别名区分。WHERE、JOIN ON 与窗口不能引用输出别名；ORDER BY 原样由 StarRocks 解析；相关子查询中本层没有的列按外层表解析，多个外层表都有时需写明表名。S1 不升级 StarRocks 数据范围摘要 `/8`；旧普通查询证据因查询策略字段变化不可读，旧计划、结构与慢查询证据仍按现有权限复核。

- `policy` 为 `allowed_functions`、`max_rows`、`max_sql_bytes` 与必填的 `max_result_columns`（P2.5 Task 3 新增，旧配置需补上）；旧版的 `allowed_objects`、`allowed_columns`、`target_id`、`default_database` 会在启动时报错，删除即可。
- `schema_limits`：`refresh_seconds` / `max_age_seconds` / `refresh_timeout_seconds`（代码默认 60 / 300 / 10 秒，主模板取 300 / 900 / 120；间隔不能大于最大年龄，刷新期限不能大于间隔）；`max_objects`、`max_columns`、`max_bytes`（每条元数据读取序列化后的字节上限）与 `max_comment_chars` 必须按目标规模填写，超过任一上限时这次刷新不生效。字段类型改为完整的 `COLUMN_TYPE` 后，长度和精度会增加列元数据字节数；升级前按目标集群的真实字段规模核对 `max_bytes` 和刷新期限。新快照超限时保留旧快照直到过期，随后该目标的数据工具不可用。
- 刷新失败时沿用上一份结构，直到它超过 `max_age_seconds`；之后该集群的数据工具暂不可用，其他集群不受影响。新授予的表在下一次成功刷新后可用；撤权后列表与表结构在返回前即发现，新查询由 StarRocks 拒绝。
- StarRocks 4.1.4 不支持列级授权：能读的表，其全部列都可能进入查询结果。需要隐藏列时，请 DBA 只授予只含允许列的视图。账号只靠未激活的角色获得权限时视为无权，需 DBA 设置默认角色或直接授权。
- 配置的审计源表经零行权限探测后进入查询范围。用户要求精确日期、Top N、统计或字段时，Agent 可直接生成 SQL 用 `run_readonly_query`，无需用户先提供 SQL；`list_slow_queries` 仍是有界的快捷诊断，只列 `isQuery=1` 且原文通过检查的记录，不列身份字段或审计表自身读取，不能替代用户指定的 SQL。
- 交付已保存的事实前，按当前权限逐个对象复核，次数受 `budget.max_scope_checks` 限制（必填，1–100000，旧配置需补上）。一轮对话中的历史回放、工具结果、最终校验与保存共用一个计数，每次复核同一集群的同一对象只算一次；轮次结束后的首次发送、历史读取与 `requests resend` 每次单独计数。用完后本次不交付，按“暂时无法确认”处理，会话与已保存结果保留，可新建会话或稍后重试。普通新结果通常经过4批复核，历史读取还需原源验证，启动仍要求该值不少于各集群 `policy.max_rows` 的5倍；失败回退和多次调用可能额外消耗次数；同一会话的历史越长，每轮需要的次数越多。

**审计源（可选）。** `targets[].starrocks.audit` 指向该集群上已有的 AuditLoader 审计表；小维不安装插件、不改其配置。各字段：

审计表全部可读列（包括 `user`、`authorizedUser`、`clientIp`、`stmt`）按只读账号权限开放给获准的 Web／飞书私聊与群聊。结果可能进入模型供应商、Session 和最长 7 天的 Evidence；查询仍受 SQLGuard、行数、单值／总字节、超时、内存与交付时权限复核约束。审计表较大，建议用 `timestamp` 限定时间范围；`NOW()` 按连接时区计算，审计 `timestamp` 按 FE 时区写入，两者不同时比较会偏移。`stmt` 经普通查询返回时可能带截短标记；需要完整原文须检查该标记，不可把前缀称作完整 SQL。

用户未指定时区时，“今天／昨天”默认按北京时间 `Asia/Shanghai`；本会话用户指定的时区优先，再换算到审计写入时区。精确条数、时间过滤与耗时排序由数据库执行，不先获取 100 条再摘要。`list_slow_queries` 没有慢耗时阈值，其 `max_rows` 默认 20、可配置至 100；分钟窗和逐条过滤的候选不能冒充全范围 Top N。直查失败保留原问题条件并如实说明，不静默改候选宣称成功。默认值和工具信息已经送达的离线验证不代表真实模型会正确选择；目标 StarRocks 的 `CURDATE()/NOW()` 与时区组合仍须真实目标验收。 普通查询返回的无时区 DATETIME 不自动补偏移；审计时间依审计写入时区解释，不能把连接时区当成所有列的写入时区。旧历史和已绑定文件可能保留原先错误附加的时区偏移；本次只纠正新查询，不追溯修正旧结果，也不批量失效历史；如需重新核对旧时间须明确要求刷新，排版追问不触发新取数。

- `database` / `table`：审计库表名（官方默认 `starrocks_audit_db__` / `starrocks_audit_tbl__`），只读账号需要该表的 SELECT 权限。
- `time_zone`：必须等于 FE 服务器的系统时区（审计时间按它写入）。配错不会报错，但时间窗会偏移，结果为空或错位；示例中的 `Asia/Shanghai` 部署时按实际核对。
- `stmt_limit`：必填，填插件的 `max_stmt_length`，并要求 `policy.max_sql_bytes <= stmt_limit - 4`，保证读出的原文没有被插件截断。
- `max_window_minutes` / `max_rows` / `candidate_rows` / `candidate_bytes`：可查的最长时间窗、最多列出的行数，以及一次取出并检查的候选行数与字节上限。

没有任何集群配置 `audit` 时不提供 `list_slow_queries`，配置开放或授予它会在启动时报错；只给部分集群配置时，只有这些集群可以使用它，其他集群的调用在连接前拒绝。读取该集群所有库的审计记录，`database` 参数不为 null 时只读执行时当前库为它的记录。原文中未写库名的表按该记录执行时的当前库（结果中的 `database` 列）解析，与 StarRocks 执行时相同；当时没有当前库的记录只接受写明库名的原文。只列出原文引用的对象、列与函数全部获准的查询，列表可能少于上限，为空也不代表没有慢查询；结果给出 `checked_candidates`、各 `omitted_*` 原因和 `unchecked_candidates`，`row_count` 仍仅表示实际返回行数。计数只覆盖本次已读取候选，`more_candidates=true` 表示候选读取已截断、之外数量未知；数据库侧 `isQuery` 与 catalog 过滤的条数也未知，需要完整审计口径时直接查询获准审计表。结果标记截断表示列表不完整：候选读满 `candidate_rows` 仍有剩余、候选达到字节上限、已列满 `max_rows` 而还有未检查的候选（即使它们最终可能都不获准），或获准的记录因单值或结果字节上限没有列出。插件刚安装后首批记录可能缺失；审计表任一行格式异常（如时间为空、指标不是整数）时整个工具失败，而不是返回部分结果。候选读取与原文检查各有一个客户端期限，最坏总耗时约为 2 倍 `client_timeout_seconds`。审计证据字段变更会使旧慢查询证据失效，无需数据库迁移。

**手动数据库安全巡检（公司验收待完成）。** 新增只读 `inspect_audit_security` 与短安全巡检 Skill。配置审计源后，由管理员将 `local/inspect_audit_security` 明确加入 `data_policy.model_tools`；个人入口加入相应 `access.grants`，指定群加入 `feishu.group.tools`。已有查询授权不自动扩大。修改模型数据策略后新建会话；无审计源不登记工具，显式诊断入口不开放此能力。定时复用规则见 [ARCHITECTURE §6](ARCHITECTURE.md#6-只读查询保护)。

在普通对话明确环境及窗口，例如：“检查 pre 的 2026-10-22 10:00–11:00（北京时间）数据库安全情况。”需要附件时再说明 Markdown 或 HTML，沿用现有报告交付。首版按小时级窗口验收，长窗可由 Agent 在预算内分窗、分别引用依据，不相加总数。工具做全窗统计、保守粗筛与有限规则精判；报告给注入／延迟线索、多语句辅助、代表 Query ID 和覆盖限制，不证明注入成功或数据库安全。超限、无法判定和样本省略会说明；全窗超长数单列，非候选超长不误报截断。该工具已移除操作类型检测；合法语法的破坏性操作若不含保留特征，不会被单独检出。它不输出审计原文，既有 `run_readonly_query` 的获准原文访问契约不变。

本地已用合成驱动/脚本模型验证调用链；公司 StarRocks 3.5.14 的谓词、容量和真实模型/渠道验收仍须按[公司侧清单](docs/starrocks-security-company-checklist.md)执行，不能用此前 4.1.4 的验证代替。检测与数据边界见 [ARCHITECTURE §6](ARCHITECTURE.md#6-只读查询保护)。

<a id="webhook-notifications"></a>

**Webhook 通知（A 已合入，真实平台尚未验收）。** 默认 `webhook_bots=[]`，不开放通知工具。启用时须有现有指定飞书群，并在 `data_policy.model_tools`、`feishu.group.tools` 和获准发送者的个人 `access.grants` 三处加入 `notifications/send_webhook_message`；只会查询数据库的群成员不会自动取得发送权。工具仅在普通群消息中开放，无登录 Web、私聊与显式诊断入口均不开放。

管理员增加以下机器人登记，`fat` 和工具名按实际已登记源/工具替换；支持多项。凭据只在服务端环境变量中配置，不粘贴到对话、配置值或日志：

```json
"webhook_bots": [{
  "bot_id": "ops",
  "alias": "ops_alert_bot",
  "description": "运维告警机器人",
  "target_version": "v1",
  "url_ref": "env:XW_OPS_WEBHOOK_URL",
  "signing_secret_ref": null,
  "receive_targets": ["fat"],
  "receive_tools": ["local/run_readonly_query"]
}]
```

默认 `protocol="feishu"`，URL 仅允许飞书官方 HTTPS 自定义机器人端点；自建代理须显式选择下述协议。两条路径均不允许用户 URL、跳转或代理环境变量。启用签名时填写签名密钥的 `env:NAME` 引用；关键词与出口 IP 白名单由管理员在真实机器人上匹配，接口拒绝时不自动加词或重试。管理员须确认 `bot_id` 对应的实际接收群；同目标密钥轮换保持目标版本，实际换群/机器人须改版本并重新授权，不清除发送记录。

群内可说“给 ops_alert_bot 发消息：今晚 22:00 检查 StarRocks。”；发送历史业务结论时，代码复核全部模型可见 Evidence 的当前权限、TTL 及机器人可接收的源/工具，事实沿 feishu 投影生成，模型说明单列。不提供主动 @所有人或成员功能；最终正文统一将 `<at …>` 的开头改为 `＜at …>`，普通文字和换行保留。外发动作事实省略只能在原群使用的 `approval_command`，原群审批材料不变。文字沿现有回答上限为16000 UTF-8字节，完成标签中和后含签名的完整请求最多20000字节；超限明确未发送，缩短内容或重新取得必要摘要，不悄悄截断。适配器使用已有 httpx2，单次总期限10秒；官方响应读取最多4096字节，代理不解析未确认的响应体。[飞书官方请求/签名契约](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)是接口依据；真实关键词、IP、签名和平台限流仍须实测。

**自建告警代理。** 同一个机器人登记项显式添加以下字段，`signing_secret_ref` 必须为 null，`url_ref` 改指管理员保存的完整代理 URL（包含唯一非空 groupId 及其他原始查询参数）；不要把 URL 或 Token 写进对话。机器人 alias、发送授权、receive_tools/receive_targets 的配置仍与上例相同。

```json
"protocol": "feishu_proxy",
"title": "小维任务通知",
"open_url": "https://dashboard.example.test/"
```

配置 title/open_url 为固定字符串，open_url 只接受 HTTP(S) 页面地址。实际请求键严格为 title/message/**openUrl**，Content-Type 为 `application/json; charset=utf-8`；不会发官方 msg_type/content 或签名。端点可使用可信内网 HTTP(S)，HTTP 的传输保密由部署网络保证；查询参数不改写/重排。切换协议、groupId 接收群或扩大接收范围时同步更新 target_version；仅同目标密钥轮换不改版本、不清空旧回执。

代理 2xx 记 accepted/proxy_http_success，只表示“代理返回成功状态，群内投递未确认”，不解析 code/errcode，不声称成员收到。响应体的业务成功协议仍未知；非2xx、超时和重定向沿同一失败/unknown 分类。用户已明确取消协议原文要求的固定 at 标签，不追加、不设开关；**不带 at 标签能否正常投递须在线上核实**。字段和路由要求来自用户提供的协议；本地20000字节上限及失败分类沿用已有契约，不能当作代理服务能力证明。后台正文超限时只发固定说明；若固定 title/openUrl 连说明也撑大到超限，则零 POST，状态可查 content_too_large，请管理员缩短固定字段。

状态区分 `accepted`（官方接口接受；代理仅 HTTP 2xx）、`failed`（明确失败）、`unknown`（可能已发送）和 `not_sent`（未发送）。同轮同机器人相同正文只发一次，不同且明确要求的正文可分别发送；failed/unknown 后不自动再发，重启也不重放。unknown 后用户新一轮主动要求重发，须先有重复风险提示；门槛由原群＋机器人共享，其他成员也不能绕过，未确认提示送达时可能先暂缓一次。原回执保留 unknown；容量降级也保留发送结果及风险，父交付失败不解除门槛。发送回执独立保存，后续模型/回答失败也不能抹掉已提交事实。API 接受不等于成员收到或阅读。

A 不在生产动作审批恢复轮自动通知；等待批准材料会提示“批准后不会自动通知，完成后请另发通知要求”。动作完成后在群里另说“通知 ops_alert_bot”，走普通通知权限和回执链。若要外发动作结果，管理员须按实际可见证据显式登记对应源的提出/状态/`request_action_execution` 接收许可；配置接收许可不授予执行写操作的权限。目的地无权接收当前查询历史时不会发送，即使发送的是纯文字；用户可显式 `/新建` 后完整重述纯文字，但这会轮换全群共享上下文，小维不会自动执行此操作。

通知和定时的应用表升级统一见上文“应用表版本17、升级与恢复”。当前保留生产发送／Action 账本并前向修复，不能恢复旧库来重置已发生的发送机会。未发布 B/C 测试候选的试验结构不构成支持的升级基线；仅对确认可丢弃的隔离库重建，不把实验步骤用于公司生产。

<a id="one-time-reminders"></a>

**一次性固定提醒（B 已合入，真实平台/模型未验收）。** 在已配置上面的机器人和指定操作群后，添加：

```json
"scheduled_tasks": {
  "enabled": true,
  "timezone": "Asia/Shanghai",
  "max_advance_seconds": 2592000,
  "lateness_seconds": 3600,
  "poll_seconds": 5,
  "max_active_jobs": 32
}
```

默认关闭。模型数据策略和群工具中加入 `scheduled/propose_reminder`、`scheduled/get_action_status`、`scheduled/get_task`、`scheduled/control_task` 及执行许可 `scheduled/activate_reminder`。创建者仍须单独取得 `notifications/send_webhook_message` 的个人 grant；确认者按现有个人 grant/群成员规则取得 `scheduled/activate_reminder`。仅要管理别人任务的管理员额外配置个人 grant `scheduled/control_task`，同时必须是当前可核验的原群成员；其他群成员不能仅凭 ID 操作。

**接收配置同样需要补齐。** 管理员若批准这个机器人在同一群会话里接收提醒、后续即时消息及任务材料，须在该机器人的登记中显式追加以下接收策略（保留实际获准的业务源/工具；`fat` 按实际替换）：

```json
"receive_targets": ["fat", "scheduled"],
"receive_tools": [
  "local/run_readonly_query",
  "scheduled/propose_reminder",
  "scheduled/request_action_execution",
  "scheduled/activate_reminder",
  "scheduled/get_action_status",
  "scheduled/get_task",
  "scheduled/control_task"
]
```

这些是接收许可，不授予调用、管理或生产写权限。提醒的提案、批准结果和状态会进入共享会话，因此只填 A 示例的业务查询许可不足以连续使用；缺少许可时拒绝并提示管理员核对，不自动扩大范围，也不要求日常连续操作清空全群上下文。显式允许 `scheduled` 仍不代表允许它引用的业务数据：发送和历史读取继续递归核对原数据源、工具、当前权限及 TTL。不同机器人分别配置；未获准接收某业务源的机器人仍不能接收携带该依赖的消息。

首次启用 B 前先完成上述登记；若扩大既有机器人的接收范围，须按 [ARCHITECTURE](ARCHITECTURE.md#scheduled-agent-tasks) 更新 `target_version`，旧任务需重新创建并确认，不自动迁移或重置发送机会。仅同目标、同范围的凭据轮换不必改版本。

示例：“明天上午10点向 ops_alert_bot 发消息：需要做汇报。”小维展示绝对日期、时区、正文、目标、有效期和迟到/容量限制，用户以现有 `/批准 <Action ID>` 材料中的实际命令确认；必须早于提醒时间，到时或之后批准会明确拒绝激活，需重新创建；未确认不发送。批准后任务有独立 ID，可问“小维，查询/暂停/取消任务〈ID〉”；暂停/取消不再次审批，不能撤回已准入的 HTTP。固定提醒到期不运行 Agent；任务列表与周期 Agent 见下方 C。当前没有任务编辑／恢复工具。

到期不调用模型；原 Session 过期不影响无历史数据依赖的提醒。若起草时已有群查询历史，全部模型可见 Evidence 都形成依赖，确认不会续期，材料展示最早失效时间。需要无旧依赖版本时由用户显式 `/新建` 后完整重述；这会轮换全群共享上下文，不能自动清除。机器人接收范围独立校验，不能把查询结果抄成提醒文字绕过。

`enabled=false` 阻止新任务起草和后台发送准入，保留状态/暂停/取消及 A 的即时通知；它不是整个服务的无网络启动模式。已激活且未开始的提醒遇停机、忙或临时核验失败，在迟到窗口内可重新核验，超窗记错过；开始后结果失败/未知均不自动重发。查询可见任务与发送状态、末次原因、连续未核实计数及时间；unknown 提示须实际送达，用户下一轮明确重发仍可能重复。这个门槛只拦即时发送，不作废其他分别批准的定时提醒；各任务仍最多一次 POST。发送截止不缩短有历史依赖的群会话数据期限。定期沿已有显式 `storage cleanup` 清理到期正文和派生副本，不新增清理 Worker。

### C：通用独立 Agent 任务

C 已合入主线，H2 已增加有界运行结果快照。正常使用当前显式升级路径，不按旧候选说明恢复 v14/v15 生产库；schema 与故障恢复以上文版本17说明为准。

在上述定时配置中增加管理员允许自动执行的业务只读工具，例如：

```json
"automatic_tools": ["local/run_readonly_query"],
"start_tolerance_seconds": 60,
"queue_timeout_seconds": 900,
"max_long_jobs_per_group": 30
```

自动集合须为已登记、模型获准的业务只读工具；启动拒绝通知、写、审批/任务管理工具。群工具与模型数据策略按需增加 `scheduled/propose_agent_task`、`scheduled/list_tasks`。激活仍复用 `scheduled/activate_reminder`，不新建审批来源。机器人 `receive_tools` 须显式包含会进入共享历史的这些 scheduled 记录，按原规则同时更新 `target_version`；这不授予机器人接收其引用的业务源。配置后先执行现有 `config check`。

例如“每3小时读取已授权源的销售统计并通知 ops_alert_bot”。小维按当前授权源/自动工具选择起草规范化任务，缺少关键业务口径才问；用户确认绝对起点、周期、任务/目标、实际只读能力、接收方和期限。预算仍由原配置约束并保存在内部定义，不默认逐项显示。未指定起点则激活后首跑，周期固定锚点；未指定有效期则长期运行。最短周期、预算和期限契约见 [ARCHITECTURE](ARCHITECTURE.md#scheduled-agent-tasks)，不把本片当作 Node 核心巡检已完成。

批准/拒绝命令支持空格或直接紧接完整 Action UUID，例如 `/批准 <Action ID>`；`/批准了吗` 仍作为普通问题。只有收到真实确认材料后才按其中命令操作。追问时可区分废草稿和待批任务；“已激活”只表示计划已启用。审批过期、主动拒绝和系统作废分别说明；旧记录无法区分原因时只说明未执行。实际期限若受历史数据限制，会保留一行原因和显式 `/新建` 后重述的办法。升级本次材料格式前尚待批的定时提案可能因既有变更校验被系统作废、未执行，需重新提出；不会自动迁移或续期。公司真实模型与群实收按[两段业务样例](docs/superpowers/plans/2026-10-20-production-dialogue-answer-repair.md#approval-task-delivery-repair)验证，本地脚本模型结果不替代公司验收。

每次使用独立 Session 和正式 Runner重新取证，旧聊天过期不影响无旧依赖定义；后台无通知、管理或生产写工具。代码核对全部实际可见 Evidence 后统一通知。错过时点跳过；准时入队的同一时点可等并发槽，排队默认900秒已由用户确认，可配置。已开始的失败/unknown不重跑；状态可查运行原因、连续未核实和发送状态，不能从“调查完成”推断Webhook已接受。结果完整JSON超过20000字节时，本次只发送固定容量说明，不发送业务正文、不拆分；发送前成员暂不可核验则零POST、累计未核实，下期重新核对。换模型或调整数据策略不自动使任务失效，每次按当前配置复核。H2 将校验通过的有界调查结果保存为带期限的运行快照，通知失败不抹掉结果；按当前权限通过历史工具读取。调用／token和耗时仍只记安全结构化日志，缺失token为未知，不另存用量副本。

可问“本群有哪些定时任务”，沿创建者/管理员可见性列元数据，正文另核对当前权限；有效期或任务内容需更改时，取消后重新创建并确认；没有独立续期工具。历史依赖不能续旧数据期限。最后一次正常结果标明期限，到期仅向原操作群发一次固定说明，主动取消不发；临时成员失败只跳过，确定撤权/退群或定义失效停止并说明。未知说明不自动重发。

**开发验证**（只用合成数据、脚本模型、替身与隔离的测试 PostgreSQL；发行测试需要本机 Docker 构建镜像，不调用真实模型或公司服务）：

选用哪种验证见 [AGENTS.md §6](AGENTS.md#6-按风险验证与交付)；日常改动不必本地全量，合并前由 CI 执行完整的默认测试集合。

环境准备：

```bash
uv sync --locked --extra dev
docker compose -p xiaowei-sdk-test -f compose.sdk-test.yml up -d --wait   # 或独立的 docker-compose
export SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres
```

相关测试示例（替换为受影响的测试文件，也可用 `文件路径::用例名` 选择单个用例）、lint 和 type：

```bash
uv run --locked --extra dev python -m pytest -q tests/security/test_workflow_policy.py tests/security/test_docs_command_consistency.py
uv run --locked --extra dev ruff check .
uv run --locked --extra dev mypy src
```

完整回归：

```bash
uv run --locked --extra dev python -m pytest -q
```

结束后清理测试实例：

```bash
docker compose -p xiaowei-sdk-test -f compose.sdk-test.yml down -v
```

`SDK_TEST_POSTGRES_URL` 只能是上面这一个测试地址；未设置时数据库用例明确失败而不是跳过。CI 的 integration job 使用同一隔离测试数据库，执行 `tests/deployment` 之外的默认测试；p3-release job 负责整个 `tests/deployment` 目录。两者共同覆盖完整默认测试集合。

自主调查与普通消息沿[同一 T1–T5 样例](docs/superpowers/plans/2026-10-20-agent-autonomy-progress.md)验收。已有交互评测脚本增加 `autonomy` 集：T1/普通消息订单统计、T2 慢查询候选和 T4 缺环境，复用正式 runtime、治理及合成数据；T3 部分监控源失败复用 `test_monitoring_read_recovery.py` 的正式入口。离线夹具的基础订单聚合使用内存 SQLite，不能证明 StarRocks 方言或优化器行为。获得模型 Profile 和费用授权后，在交互终端设置 `XW_EVAL_PROFILE` 为其文件绝对路径，再运行以下命令；现有采集器用法已验证，真实模型运行状态见 handoff，不能把离线测试称为真实评测：

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

P3-A 与 P3-B 已经独立审查并合入；精简发行包、两容器 Compose、配置预检、首次初始化与停止步骤见 [deploy/OPERATIONS.md](deploy/OPERATIONS.md)。小维连接已有 StarRocks 和模型 API，不要求在服务器部署模型；应用配置、`.env`、CA 与 PostgreSQL 命名卷由操作者保留。amd64 发行包自带应用镜像文件（`docker load` 导入，不需要镜像仓库）；已有公司安装记录，但当前版本的真实服务／恢复／用户验收仍须按 P3-C 分项核实。正式镜像使用 `Dockerfile.runtime`，部署使用 `deploy/compose.yaml`；根目录 `compose.sdk-test.yml` 只供隔离测试。

监控可靠性修订另有 `monitoring-reliability` 集，共11个场景、14轮，使用给定合成导出检验范围、历史统计、峰值/持续性、CPU口径、I/O与缺测解释，并包含证据充分、明确扩围、补查不完整和成功补查后的对照。它不证明自主取证或补查质量；[样例判据与验证边界](docs/monitoring-reliability-evaluation.md)单列。离线回归命令：

```bash
SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres \
  uv run --locked --extra dev python -m pytest tests/sdk_core/test_monitoring_reliability.py tests/sdk_core/test_dialogue_real_model.py -q
```

另获真实模型/Profile和费用授权后，才在干净基线/候选使用同一评测文件运行下列命令；当前未执行：

```bash
uv run --locked --extra dev python -m scripts.dialogue_real_model --profile "$XW_EVAL_PROFILE" --suite monitoring-reliability --repeats 3
```

<a id="tidb-usage"></a>

### TiDB 查询与诊断：已合入，待公司验收

已接入 TiDB 业务只读查询、结构/索引与现有 Web/飞书装配。已合入的 B/C 提供受保护 explain_query、query_diagnostics（慢日志、锁/事务、热点/Region/Store、组件及 TiFlash 来源）和 T8 执行前评估 Skill。已在本地 TiUP 的精确 v6.5.5 / v7.5.3 实例验证 A 查询、结构与视图路径，正式 Web 成功/报错路径使用真实数据库、脚本模型；不含 TiFlash/监控，也不证明公司 TLS/账号/容量。真实模型、公司环境与飞书效果仍按[公司清单](docs/superpowers/plans/2026-10-24-tidb-query-diagnosis.md#tidb-company-acceptance)验收，不表示可上线。

在现有配置的 `targets` 数组中加入 [TiDB 目标示例](examples/tidb-target.example.json)的整个对象。示例的 `schema_limits` 容量是业务对象与已登记诊断表及其列的合计，不是仅业务表的预算；应按合计规模配置。连接字段在 `tidb` 下；版本由 `tidb_version()` 读取，不能配置版本号。为三个集群分别填写唯一 target_id、连接信息、配置默认库和凭据安全引用；`description` / `business_context` 写清共享监控源与该集群标签。所有目标继续受现有工具/数据授权约束，没有默认集群。示例只含安全引用和虚构地址；公司实际参数由用户或 DBA 配置，启动方式沿用本文已有入口。

未限定 SQL 表名按配置默认库解析；双引号表示字符串。TiDB 会话固定 sql_mode、utf8mb4 / utf8mb4_bin 并回读，失败不发业务 SQL。用户 SQL 仅按批准规则处理外层 LIMIT，支持 OFFSET、普通注释和允许的计划类 hint；可执行注释、锁定读及资源类 hint 会被拒绝。执行后的数据库错误或未知结果中止整轮。只读账号及 SHOW VIEW 是否授予、PROCESS 与内存保护由 DBA 核实；业务快照排除系统库，不能证明安全可读的视图只排除该对象。诊断表另按登记范围发现列；query_diagnostics 需在 access.grants 与 data_policy.model_tools 中按现有规则授权 local/query_diagnostics，只接受完整限定的登记诊断表，不与业务表 JOIN。已有 describe_table 可查看其列。受保护 EXPLAIN 不运行原查询；6.5.5 的受限子查询/视图不可取计划。

<a id="cloudcanal-cc0"></a>

### CloudCanal CC0：公司现场只读契约检查

`scripts/cloudcanal_cc0.py` 是独立取证脚本，不进入 `xiaowei` 产品入口，不启动 Agent、模型或数据库。目标为用户确认的 CloudCanal 6.4.0.2；当前只实现 HTTP(S) Streamable HTTP，支持该协议的 JSON/SSE 响应。使用本仓库 Python 3.11 与锁定依赖（Agents SDK 0.22.3、MCP 2.2.0），在含该脚本的仓库副本根目录运行。

**先完成[计划 §4.1 的运行前确认](docs/superpowers/plans/2026-10-23-cloudcanal-phase1.md#41-连接与运行前置)。** 整条链接按密码保管，不贴到对话、命令参数或仓库。

以下为 **Bash** 用法。默认由 SDK 完成连接协商（可能含 `server/discover`、`initialize`）、`tools/list` 分页、GET 监听及协议会话关闭，**不会调用任何业务工具**。先关闭 shell 命令回显，用隐藏输入设置环境变量；文件放临时私有目录，带回前人工复核：

```bash
set +x
umask 077
IFS= read -r -s -p 'MCP 链接（不回显）: ' CLOUDCANAL_MCP_URL
printf '\n'
export CLOUDCANAL_MCP_URL
CC0_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cloudcanal-cc0.XXXXXX")"
uv run --locked python -m scripts.cloudcanal_cc0 > "$CC0_DIR/tools.redacted.json"
```

读完目录后，用户结合厂商文档/现场权限核实具体工具无业务副作用，再指定 **一个准确工具名和一个参数文件**。`--read-tool` 表示操作者的只读选择，不是脚本按名字或 `readOnlyHint` 自动判定安全；启停、重跑、修复及效果不明的工具不列入此次调用。参数文件为 JSON 对象，无参数也写 `{}`；文件仅留在公司，按秘密/业务材料保管，不带回原参数。

```bash
uv run --locked python -m scripts.cloudcanal_cc0 \
  --read-tool '实际已确认只读的工具名' \
  --arguments /安全目录/cc0-args.json \
  --timeout 60 --max-response-bytes 1048576 > "$CC0_DIR/read.redacted.json"
unset CLOUDCANAL_MCP_URL
```

每次最多调用该工具一次，不自动重试、不自动调用其他工具、不获取资源链接。默认网络总期限 60 秒、每个 HTTP 响应 1 MiB；连接、读取和会话关闭共用同一截止时间，到期后也不再发送关闭请求。这只是现场探查参数，不是产品配额或远端容量保证。超限后阻止 SDK 自动续接 SSE 流；超限/超时记录固定原因并退出，不能据此认为远端未执行或已停止。用户按实际契约缩小参数后取新样例，不能原样自动重放。返回中只记录固定错误类别，不打印 URL、HTTP 头、参数、异常正文或堆栈；不跟随跳转、不读取环境代理、不关闭 TLS 校验。

输出含工具原名、参数/输出 schema 的结构和约束关键字、返回结构摘要、耗时及 `wire_bytes_observed`（观察到的响应体字节，含 JSON/SSE 封装）。失败记录为累计字节，超限时可能含最后一块的超额部分。`declared_cloudcanal_version` 是用户声明版本；`received` 仅表示协议结果已收到，不证明业务成功，`mcp_error` 是远端工具错误。

业务叶子值和日志正文不输出；结果仅保留 schema 已声明的字段名，其他动态键隐藏。数组取前 3 项结构、深度到 8 层标记省略，摘要不等于完整数据。脚本不改写名称：最终报告若含完整链接/查询串（含解码形式）或至少 8 字符的路径段/查询值，只输出固定失败 `secret_in_output`，不输出原报告或命中片段。片段长度不是秘密识别依据，普通长片段可能误拒；短片段不单独检测。带回前仍须人工复核，并用无业务值的说明补齐单位、状态/错误码、分页/窗口及读取效果等语义。

退出码：`0` 为协议操作完成；`1` 为连接/读取/MCP/容量/期限失败（可保留已完成目录）或 `secret_in_output`（整份报告不输出）；`2` 为配置/参数文件错误；`130` 为人工中断、结果可能未知。只有人工复核后的报告及获准语义说明带回本地。

遇到 `connection_or_protocol_error`，在公司排查地址、网络、认证、协议和证书链。锁定 httpx2 使用系统信任库；脚本的 `trust_env=False` 忽略 `SSL_CERT_FILE`、`SSL_CERT_DIR` 及环境代理，公司 CA 应按公司规范安装到系统信任库，TLS 校验保持开启。

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

实现直接使用 [OpenAI Agents SDK](https://developers.openai.com/api/docs/guides/agents/sdk) 原生能力。旧实现只在有明确价值时提取少量业务素材，兼容旧框架不是新产品目标。

当前产品源码仅保留 `src/xiaowei`。旧 `xiaowei_agent`、专属测试、Compose/Worker/迁移入口及依赖已移除，历史可从 Git 查回；新产品仍保留旧回答和存储版本的兼容路径。旧 ADR 和里程碑仅供历史参考，不能与上述设计共同作为新产品规范。
