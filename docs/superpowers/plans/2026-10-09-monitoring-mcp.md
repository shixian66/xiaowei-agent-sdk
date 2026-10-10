# 监控 MCP 增量实施计划

> 规划基线：`5b36375e6cf8f68d02125677e174122df6775277`（2026-10-09 读取的 `origin/main`）。本文件只规定切片、验证与阻断条件；产品范围、源授权、Action 和 Evidence 的唯一权威是 [ARCHITECTURE §5「监控接入」](../../../ARCHITECTURE.md#monitoring-mcp)，通用执行和数据规则仍引用该文其他章节。当前实现与运行证据只看 [handoff](../../../AGENT_HANDOFF.md)。本计划本身不代表功能代码、真实环境或用户验收已完成。

## 1. 接手事实与最小连接方式

| 已有能力（源码事实） | 复用位置与仍需补的接点 |
| --- | --- |
| `mcp.py` 通过锁定 SDK 0.22.3 的 `MCPServerStreamableHttp` 发现/调用工具；只接受受治理映射和 JSON 对象结果 | 沿用连接、固定工具目录、调用前过滤；逐个锁版核对官方/社区工具的 schema、错误与结果，必要时只补选中工具的薄转换。不把 Server 对象直接挂给 Agent |
| `governance.py` / `tools.py` 的 `ToolCatalog`、预算、参数与目标复核；`app.py` 的单 Agent、Runner 与最终 Evidence 校验 | 按监控源登记只读工具；只为可分类的监控只读失败增加有界续查，联动本轮 `started/produced` 与最终证据检查。选中写动作只给 Runner“提出”工具，实际写工具从不入其 Tool Scope；其余执行后错误仍中止 |
| `runtime.py` 的 `StaticAccess`、`channel.py` 的交付复核；`evidence.py`、`session.py` 的数据/会话边界 | R1 已在正式 `serve` 装配 query；监控目标按现有 Grants 的工具许可取得，历史/重发本地复核。P2 沿用实际表达式与源/采集时间记录，新增范围参数与选中发现工具的最小投影，不另建授权表 |
| `feishu.py` 的指定群 `chat_id`、真实 `sender_id`、成员检查、消息去重、群排队与共享会话 | 复用入站身份和回帖；另持久化待审批 Action、审批名单、执行状态。现有群队列不能充当等人确认的长事务；Web 的共用 `operator_id` 不能批准写动作 |
| `PolicySession`、PostgreSQL 应用表、P3 备份/升级约定 | 对话继续使用 `PolicySession`；Action 提议与执行状态单独持久化到现有应用存储，批准执行不在 Runner 外写 SDK Session。下一轮经受治理只读工具查询 Action 状态并产生 Evidence，由正常 Runner 提交；当前 `get_items(limit)` 拒绝非空 `limit`，不把 RunState 恢复设为前提 |

调用链：**Web/飞书读消息 → 当前身份与监控源授权 → 同一 Agent/Runner → 本轮允许的薄 FunctionTool/MCP 映射 → GovernedTools 在网络前复核 → SDK MCP Client / 必要的 Alertmanager API v2 薄 Adapter → 结果过滤与 Evidence → 最终校验 → 按渠道/接收者投影**。群内写申请只让 Runner 调用选中动作的“提出”工具：可信代码回读当前对象、形成并保存确定 Action、返回提议 Evidence；Runner 正常完成并引用它。应用按保存内容向群投递、等 `sent` 后才接受批准，随后以审批人的可信身份重新经 GovernedTools 执行实际写工具并回读；执行事实留应用表、代码生成结果反馈，下一轮由 Agent 按权限读取 Action 状态。批准消息不得被解释成自由文本工具调用。

最小新增仅为正式运行装配、三种监控源各自选中工具的契约与结果投影、源权限/Evidence、群待审批状态及首批选中 Action。不建指标模板引擎、PromQL 解析器、独立 Planner、Multi-Agent、通用插件/RBAC 平台、自研业务 MCP Server、RunState 恢复或后台续期任务。

## 2. 各能力启用前必须定实的契约

1. **工具与源。** 每项工具固定 `server_id / tool_name / target_id / effect / 风险 / 输入输出版本`，启动发现必须与锁版契约相符；调用时再核对源、参数、预算及当前身份。Prometheus 的 `reload`、`quit`、TSDB 管理和 Grafana/Alertmanager 未选写工具不入目录。Grafana Dashboard 内的数据源引用不能借共享 Grafana 凭据绕过 Prometheus/其他源授权。若上游一个工具同时可创建/覆盖，或先返回字段 schema、再执行创建，必须按实际参数分支确认副作用并绑定准确的 Action；不能以工具名推断安全。
2. **认证与网络。** 监控 MCP Server 部署在小维节点之外，小维只建立可信配置中的固定出站连接，不在本机 Compose 中启动或维护 MCP 进程。公司可信内网的 HTTP 可用，不要求部署 CA/证书；已有 HTTPS 也可用，跨公网或不受控网络不使用本条 HTTP 取舍。R1 在现有 `MCPServerConfig` 中最小放开远端 HTTP 的校验，并保留固定端点、无跳转、预算与调用前治理；不增加 CA 管理或额外代理。代码无法仅凭 URL 判断是否属于可信内网，由操作者配置和现有网络访问控制落实这一范围。仅只读入口在网络已限定范围时可不配调用者认证；Grafana/Alertmanager 写入口必须验证调用者认证和远端选中工具限制，无认证则不开放写。Grafana MCP 的 `--server-auth-token` 对应小维已有 Bearer `auth_ref`；Alertmanager 社区 Server 不满足时改用带认证的薄 API v2 Adapter，直连原生 API 时支持目标的 HTTP Basic，用户名和密码取安全引用，不把现有 MCP Bearer `auth_ref` 当作 Basic。Prometheus 官方 HTTP MCP 会转发调用方的 `Authorization` 到 Prometheus；其可选 Web Basic Auth 与转发共用该头，G0 实测带头/不带头，不能假定两层身份隔离。在内网 HTTP 下认证头和业务内容均为明文。Grafana MCP 的使用统计在外部部署参数中显式关闭。
3. **读结果与失败。** PromQL、时刻/范围/步长、源与采集时间由可信代码记录；成功证据只对应真实成功调用。Prometheus `warnings`、部分数据与截断须显式传给回答；Dashboard 定义不能冒充实时数据。只有上游 `errorType` 与响应可可靠判为语法/表达式错误时才允许 PromQL 有界修正，不能只凭模糊文本或单个通用错误码。选中只读工具的超时、不可达、上游 5xx 与可确认的上游 401/403 按固定类别交模型续查其他已授权源，计预算、无成功 Evidence、本源本轮不再调用；本地授权拒绝仍在 I/O 前完成。同步本轮执行记录和最终校验，由代码在交付时附失败源说明；历史与重发重新核对失败源当前授权。无成功证据时可给明显标为“未经数据验证”的排查建议，代码展示失败源，不作当前健康结论。分类不明、协议/投影/Evidence 失败及任何写失败仍中止。表达式内部窗口和高基数由 Prometheus 服务器限额兜底。
4. **写状态。** Action 在现有 PostgreSQL 应用存储中保留待审批、已拒绝/过期、执行机会已占用、成功或结果未知等足以区分下一步的状态，具体枚举由开发切片确定。提议 Evidence 与最终回答校验失败时 Action 不可批准；群交付未处于 `sent` 也不可批准。执行机会参照现有投递 claim/实例锁机制做持久抢占，旧实例失去锁后不得执行。飞书重投、多人同时批准、进程重启和网络断开不能导致二次写。审批入口只依赖有效 Action/群身份和执行锁，不以群 Session 仍可提交为执行条件；执行与回读的可信事实以 Action 应用表为准，不在 Runner 外写 `SQLAlchemySession` 底表。下一轮的状态只读工具须按群、监控源和当前权限生成 Evidence，再经现有数据策略、历史上限和 Session 状态检查保存。每个动作的确认材料、回读与补偿按架构 §4/§5；无权审批、关键条件变动、冲突、未知结果在写请求前或执行后按相应状态终止。群确认只准对应本次 Action，不准“以后都同意”。
5. **数据与权限。** 源级共享许可不等于无边界：监控源由现有 AccessPolicy/StaticAccess 配置授权给人/群，批准名单从同一授权配置取得，不建第二套授权源；远端服务账号必须限制到同一 Grafana 组织和获准上游。历史按当前源、工具及接收权限本地复核，不依赖每个 Dashboard 的远端 ACL；Grafana 暂不可达不应使整个群 Session 无法回放。四种投影、Evidence 保留、Session/模型外发照现有 §9。数据源创建/修改不得接收或展示密码、Token、`secureJsonData`；创建后“待配置凭据”，由用户去 Grafana UI 填写。对 Grafana 数据源 URL/插件连接能力，必须核实远端网络出口，不能靠群确认代替服务端隔离。

## 3. 实施切片与验收

**依赖顺序：G0 → R1 → P2；R1 后 G3、A4、W5 可按环境独立推进；S6 依赖 A4 + W5，D7/C8 依赖 G3 + W5，E9 汇总全部能力。** G0 只验证共用 MCP 接入的代表路径；各源和写动作的选中工具在自己的切片启用前验证，不让尚未开发的 Grafana/Alertmanager 工具阻塞 Prometheus 读取。Agent 调查时的工具选择与顺序按 [架构的自主调查约定](../../../ARCHITECTURE.md#monitoring-mcp)。每片以具体源码差异、适用的离线/真实证据和交接状态独立复审；不以后一片的通过覆盖前一片缺口。

| 切片与可审阅结果 | 正常场景：用户操作 → 系统行为 → 用户所见 | 关键失败与验收证据 |
| --- | --- | --- |
| **G0 共用协议门槛**：用一项 Prometheus 只读工具核对锁定 Server、SDK 与小维的最小接入契约 | 开发者对合成目标调用代表工具 → 真 SDK 客户端发现、调用并做必要的薄转换 → 看见符合 JSON 对象契约的结果 | 核对输入 schema、`is_error`、文本/structuredContent、错误与返回上限，以及固定端点、所选 HTTP/HTTPS、关闭、超时和实际认证传递；分别记录不带 `Authorization` 与带 `Authorization` 时 Prometheus MCP 的行为及上游身份。不兼容则先修正映射；Grafana、Alertmanager 与写工具各在对应切片核对 |
| **R1 源级装配/授权/Evidence**：R1a 已随 PR #67 合入；R1b 重连已合入主线，真实外部节点待验证。正式 `serve` 能选配已映射的监控源，空配置继续启动 | 获准用户在 Web/飞书问已接入源的内容 → 本轮只显示对应读工具、调用前复核、结果进入 Evidence → 看见来源/时间；外部 Server 重启 → 后续轮次按源有界重连并重新核约 → 恢复后可继续查询，无须重启小维 | 未授权源、按架构 §5 停旧进程后配置撤权、跨源引用、MCP 断开、schema 漂移：零未授权 I/O、无越权历史/重发；重连失败按源指数退避、不变更进行中工具集合、不自动重放调用；`/readyz` 列出源状态且 StarRocks 可用。真实 PostgreSQL + 真 SDK + loopback 与固定内网 HTTP 合成 MCP 检查启动不可达、运行中断开与恢复、读取投影、会话、证据、端点/认证头；源级历史本地复核在 Grafana 不可达时仍可回放获准群 Session；旧配置和旧证据兼容按 §4 验证 |
| **P2 Prometheus 自主调查**：发现指标/标签/主机/已加载规则，instant/range PromQL；只读故障续查先行作为本行的独立子片 | 用户在 Web 或群问主机 CPU/内存/Kafka 状态 → Agent 自主发现并生成 PromQL，按源/时间查询 → 显示实际表达式、采集/求值时间、有限数值和推断 | 先核对选中工具的真实 schema、Prometheus `errorType`/错误内容与结果；仅可确定的语法/表达式错误允许有限修正，错误计预算无成功 Evidence，修正后真实成功才生成 Evidence。超时/不可达/上游 5xx/401/403 可标明该源未核实并续查其他已授权源；本源本轮不重试。未知/协议错误中止，本地无权调用零 I/O；全部读取失败可给明确未验证的排查建议，不报“健康”。warnings/截断及预算耗尽不报“健康”，越界时间/点数/长度零网络请求。真 SDK + 协议替身、获准真实 Prometheus 验服务端 timeout/max-samples/max-concurrency 与高基数失败；三源联合问题样例校准 `max_tool_calls/max_turns`，固定模型任务样例覆盖不同指标，不建指标白名单 |
| **G3 Grafana 只读调查**：Dashboard/Panel/数据源元数据与历史权限 | 用户在飞书问 Dashboard 面板含义 → Agent 按问题选工具，可读定义并关联获准 Prometheus 实时结果 → 分列显示定义和实时证据 | 首批只核约所需只读工具（Dashboard 搜索/读取、数据源元数据、Annotation 读取），这不是固定调用顺序或永久对象白名单；逐项核对 schema/结果。无源权限、当前读取失败、面板引用未授权数据源、版本/内容过大：拒绝读取或只报告定义且标明未查实时；历史回放/重发按当前源和接收权限本地复核，不为每个对象远端查 ACL。锁版真 Server + 合成/获准 Grafana 核对响应，禁止未授权代理查询 |
| **A4 Alertmanager 只读调查**：告警/静默/接收器和状态，确定社区 Server 或薄 API v2 Adapter | 群员问某告警是否被静默 → Agent 读独立 Alertmanager → 展示匹配依据、静默 ID/起止时间和采集时间 | 先核对候选 Server 的所选只读工具和 API v2；写入口若缺认证或远端工具限制，S6 改用经认证的薄 API v2 Adapter。目标不可达、分页未取完、静默已到期、权限撤销：不称“无告警/未静默”；只读调用不暴露写工具。取消静默的实际 API 行为留在 S6 验证 |
| **W5 群审批底座**：仅为已选监控动作持久化候选 Action 与批准状态 | 当前群成员申请具体动作 → Runner 调用受治理“提出”工具，可信代码回读对象/版本、规范化参数、生成差异与 Action/提议 Evidence，Runner 正常结束并引用证据；应用按已存内容交付群且记录 `sent` → 仍在群内的名单成员 @ 机器人逐次批准 → 应用绑定批准并进入可执行状态；批准后群收到代码生成的执行反馈，追问时 Agent 用只读工具查询 Action 状态 | 提议 Evidence/最终校验/群发送失败、Web/单聊/其他群、退群/非名单、伪造 open_id、过期、撤权、参数/目标变更、重复或并发事件：远端写 I/O 为零；审批入口确定性解析并去重，不进模型。真实 PostgreSQL + 真 SDK + 飞书事件替身验证提议工具调用/结果成对进入 Session，旧实例失锁后不能抢执行机会、同群执行与 Agent 轮次串行、重启后不自动补写；审批人身份的仅此写工具 Tool Scope 经 GovernedTools 和已有 MCPIntegration/Adapter 执行，结果以应用 Action 表为准。状态只读工具验证群/源/Action 归属与当前权限，生成 Evidence 并由下一轮正常提交；Session 已满、关闭或保存失败时执行事实仍保留、不重复写，用户新建会话凭 Action ID 仍可获准查询；群队列不因等待审批而阻塞 |
| **S6 Alertmanager 静默写闭环**：创建与取消静默 | 群员申请覆盖某告警 1 年/3 年/明确到期的自定义时长 → 应用在群审批材料中列出匹配器当前命中的告警数量和有界样例（无法完整计数则明示） → 有权成员批准 → 创建并回读，群显示实际起止时间；取消某静默 ID 经另一次批准后回读提前结束状态 | 先核对所选 Server/API 的创建、更新和按 ID 取消分支。匹配器意外扩大、结束早于开始、取消了错误 ID、审批失效、重复回调、上游超时/结果未知：拒绝或待核实，不自动重发；取消前展示并核对 ID、当前匹配器与影响，已到期不谎报“刚取消”。走薄 API v2 Adapter 时按目标 Web 配置验证 Basic 用户名/密码的安全引用、无/错凭据零写入及正确凭据仅执行批准动作；现有 MCP `auth_ref` 只发 Bearer，不当作 Basic。社区 MCP 若不能隔离选中工具则用经认证的 API v2 Adapter。隔离 Alertmanager 实跑创建/取消、读回状态及告警抑制；路由与时间决定是否发后续通知 |
| **D7 Grafana Dashboard 写闭环**：创建与修改 | 群员申请创建/修改 Dashboard → 群显示目标、完整结构化差异、版本和恢复方式；批准后提交并回读 → 看见 UID/版本及结果 | 先核对锁版 Server 的创建/覆盖参数分支、`--disable-write` / `--enable-write-tools` 的实际注册结果与返回；未选写工具用正确凭据直调仍不存在或拒绝，无/错凭据零写入。外部先改版本、上游写后读失败、内容超限、越权文件夹/组织、重复批准：冲突拒绝或结果未知，不覆盖新版本、不自动重写。更新可通过新一次获批准的更新恢复前版；创建若需删除，首批由 Grafana 管理员按 UID 人工恢复。隔离 Grafana 实跑、版本冲突与备份证据 |
| **C8 Grafana 数据源与 Annotation 写闭环**：两类能力可分别启用/验收 | 群员申请创建/修改非秘密数据源或新增 Annotation → 群显示精确字段/URL/对象和影响，批准后回读 → 数据源需密码时显示“待配置凭据”和 Grafana 设置入口，Annotation 显示 ID | 先核对数据源建前 schema/实际写入和 Annotation 工具的参数分支，确认选中写工具以外的操作不能用正确凭据直调；无/错凭据零写入。secret/未知字段、URL 带凭据、Grafana 出口可达未授权内网、错误插件类型、Annotation 重复/时间错误：拒绝、隔离或结果未知；健康检查前数据源不称可用。更新旧数据源可经另一次获批准的更新恢复；新建数据源/Annotation 需删除时由 Grafana 管理员人工恢复。隔离 Grafana/受控出口实跑、无凭据泄漏检查，逐能力开关 |
| **E9 真实入口与运维验收**：一版可试用的监控报告及群审批 | 用户从 Web/飞书调查主机及关联告警 → Agent 按问题选择监控源，联合调查样例覆盖三源并给带实际 PromQL 的报告；群成员提出首批写 → 名单成员批准 → 回读结果与群反馈 | 真实模型、真实三源、指定群、撤权、重复飞书事件、超时/中断/重启、协议变化、版本回退、历史复核和隔离库恢复分别留证；三源联合样例校准预算，任一读源 5xx 后仍能报告已有证据和未核实部分，预算耗尽提示不完整；审批申请人可自批的用例单列。未取得真实环境或用户验收时只标离线完成 |

每片最少验证当前源码所改的正常/拒绝路径、受影响 lint/type、协议和存储边界；权限拒绝必须断言**实际远端 I/O 为零**。模型/提示词变化另用获准模型固定问题样例比较取证充分性、事实/推断、实际 PromQL 与资源开销，允许不同的合理调查顺序，不把某条工具调用序列写成通过条件。部署、真实入口和用户接受按 [AGENTS §6](../../../AGENTS.md) 分级，不用模拟器宣称实战。独立审查写精确提交 SHA 和覆盖范围，不把自查写成独立复审。

### 3.1 P2 当前切片（基线 `4c5b89495fdf57bfd6d18bb4483d5682e56cce59`）

PR #70 的只读故障续查已合入。本片只扩展 Prometheus 读取，不改写能力。先用官方 v0.18.0 核约，再沿 `serve → Web/飞书 → Runner → GovernedTools → MCP → Evidence/PolicySession → 交付` 实现。独立审查以本节与协议用例的具体提交为准；不合入实验分支。

**Agent 的任务和选择。** 用户问“host1 最近为什么 CPU 告警”“这台主机叫什么、监控里能看到多少 CPU/内存”“Kafka 积压是否和已加载规则有关”：Agent 自选获准源、指标/标签、发现方式、表达式、时间窗、步长和调查顺序；可直接查已知指标，缺关键业务含义时才澄清。主机配置仅报告指标实际提供的事实，不推断未采集的硬件配置。失败或信息不足时可换已授权源、补证据或结束；不要求每次先发现、先读规则或固定工具顺序。代码管授权、资源、真实证据和分类边界。

| 接口（远端名 / 策略后缀） | 本地必填输入与输出约定 |
| --- | --- |
| `query` / `query` | 保持 R1 的 `{query}` 输入和结果契约/指纹，兼容旧证据；服务端即时求值。实际表达式由代码补入，排版文本中的样本时间原样展示；采集时钟不冒充求值时间 |
| `range_query` / `range_query` | `query, start_time, end_time, step`；时间可为 Unix 秒、含时区的 RFC3339，或 `now` / `-15m` 等单单位相对时间。调用前冻结为实际 Unix 秒和秒步长，结果记录冻结值。起止相同时可查明确时刻；返回排版文本与 warnings，不解析数值或 PromQL |
| `label_names` / `label_names`；`label_values` / `label_values` | 均含 `matches, start_time, end_time`；后者另有 `label`。空 matches 表示源内发现；返回排版文本/warnings 与实际时间窗 |
| `series` / `series` | `matches, start_time, end_time`；至少一个选择器。返回标签集排版文本/warnings 与实际时间窗；不称为主机资产清单 |
| `metric_metadata` / `metric_metadata` | `metric`（空串表示发现全部元数据）；不发送可选 limit，沿用 Server 默认。元数据 JSON 包为固定 `metadata` 字段，类型/help/unit 做声明式投影 |
| `list_rules` / `list_rules` | 无参数；只读当前已加载规则，投影 group/name/query/labels/annotations/state/health/lastEvaluation 等必要事实。Server 不返回 type；不以猜测补上。health 是规则求值状态，不是主机健康 |

策略 ID 均为 `prometheus.<后缀>`，工具/目标 ID 沿用源的静态映射和 Grants。只可配置本表的准确映射与子集；空配置、旧 query 配置和旧 query/StarRocks Evidence 不变。SDK 严格输入的字段均必填。只对本地非空数组、远端 `type:[null,array]` 且其余约束相同的情况接受类型子集；反方向、元素/约束漂移仍隐藏工具。其余未暴露可选参数不发送，默认行为见锁版用例，不引入 schema 通用转换器。

**实际协议依据。** `tests/sdk_core/test_prometheus_p2_protocol.py` 使用官方 v0.18.0（源码 `924e43dbea816b92c3e79e73c1568af417e8e30e`）、真 SDK 和 loopback API，逐项断言 schema、结果及每次上游请求数加 1。本机 darwin/arm64 二进制 SHA-256 为 `6272e0d8fbc8af341d1e93fdc6aa130f7086b7a993faedb015966c2ace5e3a82`，不同于发布压缩包校验值。query/range/标签/series 返回 JSON 包裹排版文本；metadata/rules 丢弃上游 warnings，固定说明这项完整性限制。已核实 truncation 标记：文本工具标记在 result 内，metadata 标记可能追加在 JSON 之后；只接受锁版完整标记，其余尾随内容拒绝。标记或投影截断设置 truncated，空结果不声称健康；未知结果形状仍中止。正常部署继续推荐 `--prometheus.truncation-limit=0`。

**失败与资源契约。** 所有本表只读工具沿用已批准的源级 auth/timeout/unavailable/upstream_5xx 续查，不自动重放；当轮停用失败源，可继续其他获准源。仅 query/range 的锁版单段错误，完整匹配工具前缀、`bad_data: invalid parameter "query": 行:列: parse error: …`，才交回可信的解析失败类别与位置；不透传原始错误。每源每轮最多 3 次已执行的语法失败（初次及最多两次失败的修正），同一失败表达式不再发出；失败计预算、无成功 Evidence，成功后才生成证据。bad_data 的 step/标签错误、422 execution、未知文本、多段/结构化错误、协议/投影/存储失败仍中止。最终校验分别统计成功查询、分类的源故障和语法失败，不能靠忽略失败调用通过；发现事实由 Agent 按需要引用，已成功执行的 query/range 仍全部引用。

I/O 前复用 `Prechecked`：表达式/选择器最长 8192 字符、最多 32 个选择器；时间须有限且起止有序、窗宽不超过 31 天；step 至少 1 秒，单序列求值点数 `floor((end-start)/step)+1` 不超过 11000；label 非空且符合标签名语法。拒绝零上游请求并说明可修正原因，Agent 在范围内自选参数。以上是客户端硬上限，不限制表达式内部回看/扫描量；真实启用前仍须核对服务端生效的 timeout/max-samples/max-concurrency，据容量收紧，未核实时不标真实验收通过。

PR #70 的三个非阻断项在本片同一失败契约中落实：并行成功/失败的说明只称该源部分读取未核实，不否定成功调用各自范围；全失败且步数耗尽使用现有 SDK 无工具收尾，明确未验证/不完整；`commit_validated` 再次校验带同一份 monitoring_failures，撤权后不可提交。

| 顺序与可独立验证的结果 | 成功/关键失败验收 |
| --- | --- |
| P2.1 锁版协议与本节审查 | 真 Server 覆盖七工具、带/不带认证、原样时间、warnings 缺失及截断；400 query 与 step 反例、422/401/403/500/503 分开断言；调用恰一次，无重试。审查通过后再改产品代码 |
| P2.2 正式装配与最小用户闭环 | 先补失败测试，再用正式 serve/Web 查 range 或发现，真实 PostgreSQL/SDK/Evidence/Session；先跑一成功、一越界零 I/O 拒绝。子集授权、固定端点、schema 漂移、跨源引用、旧配置/旧证据兼容；query 与发现输出不混称数值 |
| P2.3 自主修正与失败收尾 | 真 Server + 正式治理链验证坏表达式→模型改写→成功证据，重复表达式/次数/预算上限、错误隔离变异；故障换源、全失败无证据建议、并行成功/失败、提交时撤权；正常/拒绝均覆盖历史和重发 |
| P2.4 能力评估与交付 | 正式浏览器与群事件替身展示来源、实际表达式/冻结时间、warnings/截断和推断；R1/MCP/G0/SDK 相关回归、Ruff/type 检查。获准真实模型上评估上述三业务问题及一次语法修正，不固定顺序，记录答案/取证/澄清/调用次数/耗时/用量；真实 Prometheus 验扫描限额。缺模型或真实源继续离线并留下能力/环境验收门槛，不用脚本模型豁免；提交、推送并开 PR，不合并/部署 |

## 4. 环境、兼容与恢复

- **开发/协议：** 锁定 `uv.lock` 中 SDK 0.22.3；隔离 PostgreSQL、真 SDK Runner、loopback MCP fixture。每个源的切片才准备对应的可丢弃 Prometheus/Grafana/Alertmanager 实例，并以所选 Server 的**具体发布版本/镜像 digest**核对其工具清单与响应，不从当前 `main` 文档推断未来固定镜像的行为。社区 Alertmanager Server 若传输、鉴权或返回契约不合适，A4 选择 API v2 薄 Adapter 并记录证据，不引入第四个常驻服务。官方来源：[Prometheus MCP](https://github.com/prometheus/prometheus-mcp)、[Grafana MCP](https://github.com/grafana/mcp-grafana)、[Alertmanager API v2](https://github.com/prometheus/alertmanager/blob/main/api/v2/openapi.yaml)、[社区候选](https://github.com/ntk148v/alertmanager-mcp-server)。
- **目标环境：** 外部节点负责所选 MCP Server 的安装、版本、上游连接和生命周期；可按实际运维安排共用外部节点，但不进入小维的两容器 Compose。Alertmanager 若走薄 API v2 Adapter，小维直接连接外部 Alertmanager API，不在本机运行替代 Server；其 Basic 用户名/密码另由安全引用装配，不扩大全体 MCP 的 `auth_ref` 语义。每个源启用前再取得该源从小维容器可达的固定端点、服务账号实际权限、内网访问控制和可发模型/群的数据范围；Prometheus 切片核对查询限额，Grafana 切片核对组织与出口，W5 在现有授权配置中加入指定群审批人 `open_id`，不另造按源授权层。运维配置尽量限于源的 `server_id`/地址/所需认证引用、现有授权表的工具 ID 与审批名单。写入口缺少经验证的调用者认证或无法限制未选写工具时不开放写；只读入口可依靠受限内网。内网 HTTP 无须准备 CA，但认证头明文传输；选择 HTTPS 时验证证书信任。只在对应运行环境的安全引用中配置凭据，不在计划、群消息、日志或测试快照写值。Grafana MCP 关闭默认匿名使用统计；不靠小维客户端限时来声称 Prometheus 服务端扫描已受控。[Prometheus 资源参数](https://prometheus.io/docs/prometheus/latest/command-line/prometheus/)、[Grafana MCP 部署说明](https://github.com/grafana/mcp-grafana/blob/main/README.md)、[Alertmanager Web 认证](https://prometheus.io/docs/alerting/latest/https/)。
- **兼容：** 保留监控配置为空时的旧启动行为；旧 StarRocks 工具/证据不随新监控目标自动授权。新证据与 Action 用版本化契约和数据库迁移；旧进程不识别新版本应用表时拒绝启动。Server 版本或 schema 改动先在隔离环境重验，未通过时只关闭相应工具/源，不扩大默认权限。Grafana 的 provisioned Dashboard/数据源若目标 API 不允许修改，作为能力不可用明确报告，不绕过 provisioning。
- **恢复：** 上线前按现有 P3 运维流程备份 PostgreSQL 和操作者配置；小维保留旧镜像，外部节点各自保留受测 MCP Server 版本与回退办法，先读再逐项开放写。升级失败停候选、恢复旧程序所需的数据库备份及原配置，待审批/结果未知 Action 先做只读核实，不自动补跑。外部 MCP 故障只关闭受影响监控源/工具，不阻断 StarRocks；后续轮次按源有界重连、重新核对工具契约，写工具失联则关闭该动作并留可诊断状态；更换源凭据、撤权或回退后复核旧 Evidence。每种已执行动作的补偿见架构 §5，人工恢复的 ID/操作者/结果必须记录，不能把恢复计划写成已回滚。

## 5. 尚未解决的正确性门槛

| 疑点 | 本计划默认处理与决定时点 |
| --- | --- |
| 锁定 SDK 的提议工具、Evidence 与 Session 提交，能否按已定 Action 契约原子地保证“未成功交付就不可批准”？ | W5 用合成动作、真 SDK 与 PostgreSQL 验证 Action/Evidence/最终回答/群投递失败的每个窗口；只有已保存提议 Evidence、有效最终回答及 `sent` 交付可批准。执行仍复用 GovernedTools 与既有 MCPIntegration/Adapter，不恢复 Runner、另起模型工具循环或绕过治理。路径不成立则阻断写切片，不影响只读 |
| Action 执行发生在 Runner 外，下一轮如何看到结果而不绕过 PolicySession？ | W5 已选受治理的只读 Action 状态工具：应用表保存执行事实、群反馈代码生成；下一轮按 Action ID、群和当前源授权产生 Evidence，再由正常 Runner/PolicySession 按数据策略、历史上限和状态提交。模拟已有效提议后群 Session 达轮数/字节上限、关闭、查询保存失败与新会话查询；批准执行仍按有效 Action/群身份处理，不得直写 SDK 底表，也不得因会话错误丢失或重放 Action |
| 选中只读 MCP 工具的超时、不可达、上游 401/403/5xx 和 PromQL 表达式错误，是否能从锁版 Server 响应中可靠分类？ | G0/P2/G3/A4 用实际错误样本验证；不能可靠分类的结果继续中止。可续查失败须计预算、无成功 Evidence、本源本轮不重试，并同步最终查询完整性检查；测试部分故障后现有证据可交付且由代码标明未核实，全部读取失败时可交付明确未验证的建议与代码生成的失败源。本地授权拒绝、协议/Evidence 与写失败仍按原边界处理 |
| 锁定 SDK 的公开 MCP 生命周期能否在不修改进行中 Runner 工具集合的情况下按源重连？ | R1 以启动失败、运行中断、恢复和并发轮次验证；仅后续轮次重新发现并核对契约，不自动重放调用/写动作，不引入后台探测器。若公开能力无法做到，先停 R1 并明确需要改变的运维取舍，不以无界重试代替 |
| 官方/社区 MCP 的选中工具实际 schema、错误载荷、写幂等性、回读字段与 `is_error` 是否满足严格 JSON 对象契约？ | G0 只验证代表路径；P2/G3/A4/S6/D7/C8 分别验证各自要开放的工具，转换只做获准工具。社区 Alertmanager 不合格则 A4 选择薄 API v2 Adapter；未知结果不自动重试 |
| Grafana `create_datasource` 的建前 schema 查询、Dashboard 创建/覆盖，以及 Alertmanager `post_silence` 的更新能力，能否与选中 Action 明确分开？ | S6/D7/C8 按锁定版本验证各自参数分支；无法在网络前证明某次调用是只读或批准的准确动作，就不开放该调用，不靠 MCP 的 `readOnlyHint` 判断 |
| 外部 MCP 的固定内网地址能否从小维容器访问，且部署网络能限制可达范围、避免 Prometheus `Authorization` 意外转发？ | R1/相应源真实环境确认；内网 HTTP 可用、无需 CA；仅只读入口在网络受限时可无 MCP `auth_ref`，写入口须验证认证与远端选中工具限制。若选择 HTTPS 才检查证书；端点无法固定或网络不受控时该源不启用 |
| 锁版 Grafana/社区 Alertmanager Server 能否直接拒绝无/错调用者凭据及未选写工具？ | G3/A4 查实际配置，S6/D7/C8 在隔离环境按三种直接访问做零写入用例；当前 Grafana README 的 `--disable-write` 列表未列 `create_datasource/update_datasource`，不能凭开关名推断它们受限，必须查锁版注册表并直调验证，连同通用 API 旁路。社区 Alertmanager 不满足则只用经认证的薄 API v2 Adapter；Grafana 不满足时不开放该写能力，先核对锁版能力及最小替代路径 |
| 目标 Alertmanager API v2 的 Web Basic 与所选社区 MCP 的认证形态是否符合写入口契约？ | A4/S6 锁版核对目标 Web 配置；薄 Adapter 用安全引用装配 Basic 用户名/密码，分别验证无/错凭据零写入、正确凭据批准后读回，不把当前 Bearer `auth_ref` 复用为 Basic。社区 MCP 若另有认证协议单独验证，不能继承原生 API 的结论 |
| Grafana 的共享服务账号、组织 ACL 与新数据源可连到哪些内网地址，是否与源级共享授权相符？ | G3/C8 以真实环境核实；不满足则不开放相应源或数据源写，不建立对象白名单来掩盖出网越界 |
| Prometheus 当前生效的 timeout/max-samples/max-concurrency、可接受的查询时间窗与点数是多少？ | P2 前由监控环境负责人提供并验证生效值，据容量在可信配置中定阈值；无服务端边界则不能开放自由 PromQL |
| 目标 Grafana 版本的 Dashboard API 是否支持所需版本冲突拒绝、历史回读与按组织权限控制？ | D7 实测；不能证明不覆盖他人版本则不启用修改动作 |
| Grafana 数据源更新 API 是否提供原子版本条件？ | C8 实测；至少批准前及写前比对当前配置摘要。若无原子冲突拒绝，记录真实竞争窗口，生产启用前由用户明确接受该残余风险或改变范围，不能宣称完全避免并发覆盖 |

上述门槛由技术验证与环境事实决定，不把用户已确认的源级共享、群审批、自批、长时有限静默和首批动作重新当作待决产品选择。
