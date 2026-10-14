# 自然回答与完整结果交付修复实施计划

> 文档版本：v1.1，2026-10-14，Asia/Shanghai。源码基线 `0c54442fbd15d92e2d9fc544b6e7cddd8cc0cbca`（本地 `origin/main`，含 PR #99）；文档候选的精确 SHA 以包含本文件的 Git 提交为准。
> **执行说明：** 产品实施须另获授权；默认按本计划串行交付一个可验证切片，执行时使用 `superpowers:executing-plans`，仅在用户选择后使用 `superpowers:subagent-driven-development`。本次只写设计/计划/必要文档及离线核查，不写产品功能代码，不推送、合并或部署。

**目标：** 普通回复简洁自然；本轮已取得且获准交付的结果和分析完整可得；HTML/MD 在同类场景共用可读展示；投递部分失败可定位和恢复。

**架构：** 保留单 Agent、真 Runner、output_type、治理、Evidence、Session 和渠道 SDK。扩展现有结果投影、文件生成与请求投递链，先闭环完整交付，再改通用文档和消息排版，监控结构化曲线单独验证。

**技术栈：** uv.lock 的 OpenAI Agents SDK 0.22.3、lark-channel-sdk 1.4.0、markdown-it-py 4.2.0，现有 PostgreSQL/SQLAlchemy、Python 标准库；当前应用表 v11。没有新运行依赖的计划。

**设计来源：** [ARCHITECTURE §7 自然回答与完整结果交付](../../../ARCHITECTURE.md#result-delivery)。产品取舍、数据/权限、容量和分项状态契约只在该节维护；本文件只维护源码核查、任务、依赖、验收和疑点。[AGENTS](../../../AGENTS.md)规定开发和独立审查；已有工具/查询契约引用 ARCHITECTURE §5–§6，不重复扩写。

## 1. 已核实根因与最小方案

| 当前调用点 | 已核实事实 | 复用与必要调整 |
| --- | --- | --- |
| `app.py` → `Runner.run` → `AgentAnswer` | 模型选择引用/推断，report_format 目前要求显式报告；所有成功业务查询必须被引用 | 保留原 Agent Loop；report_format 表达格式偏好，自动附件由容量代码决定，格式不触发新增取数 |
| `governance.py` / `mcp.py` / `starrocks_tools.py` → `EvidenceStore` | 先治理后 I/O，按四种用途投影；原始结果不长期保存 | 继续同一链；完整性优先从可信来源标记与既有计数生成，不恢复丢弃数据 |
| `evidence.py` → `DeliveryFact` | 原值/类型在 result_json；rows 含浏览器显示转换；brief 仅历史事实 | 附件使用获准原结构；完整交付集合不能受正文 brief/预算裁剪影响 |
| `feishu_render.py` | 单条 30,000 字节本地请求预算，行/分析可裁剪，容量说明也可能落 sent | 保留安全组件与请求测量；改成先完整构造、再选载体；审批完整材料仍独立保护 |
| `feishu_report.py` | 只接受有 facts 的只读报告；固定巡检标题；MD 围栏与 HTML pre；400,000 字节/10,000 行；brief 原文省略 | 扩展现有模块的段落/表格/代码处理；先复用当前保真模板闭环交付，再改善布局，不先建模板注册平台 |
| `send_delivery` / `ResultDelivery.send` / `channel_store.py` | 文件绑定原字节/键；整轮只有 sent/failed/unknown；transport 丢弃成功平台 ID | 在现有存储与发送边界补必需部分记录、平台回执与语义版本；保留原占位、防重、owner 与恢复保护 |
| `runtime.py` / Prometheus MCP | range_query 的已登记 result 是字符串；Grafana summary 是配置摘要；R4 已有慢查询遗漏原因 | 曲线先验证锁版协议能否可靠提供采样结构；R4 计数直接复用，不重新发明候选完整性 |

仅加 CSS 无法解决裁剪、brief 文件与部分投递；新增文档平台则没有直接消费者。选择扩展共同根因所在的现有链，普通内部函数、辅助模块拆分和签名由实施阶段决定，不为名字变更全仓迁移。

### 1.1 SDK 公开能力与离线证据

本次在仓库现有独立 Python 环境中进行无网络核查，使用本计划基线的 models；不是新功能/真实服务验收：

- `Agent(output_type=AgentAnswer)` 与公开 `AgentOutputSchema.validate_json` 可保留带自然段落的结构化分析并选择 html；SDK 验证类型，不负责 Evidence 真实性或完整送达。[官方 Agent 定义](https://developers.openai.com/api/docs/guides/agents/define-agents)。
- `FeishuChannel.send(to, message, opts=None) -> SendResult`、`upload_media(source, *, kind, file_name, file_type) -> str` 可直接复用；文件支持使用已上传 key，最终 SDK 返回 message_id。
- SDK 公开导出的 `markdown_to_post_ast(..., tag_md_mode='native'|'structured')` 两种模式均有转换结果；合成 SQL 在 structured 的 code_block 中逐字相等。native 返回 md 节点，效果与复制仍需客户端验证。[官方 Markdown 文档](https://github.com/larksuite/channel-sdk-python/blob/main/docs/markdown.md)。
- `SendResult` 有 success/message_id/error/raw/chunk_ids。锁版 sender 的多段成功返回全部 chunk_ids；中段失败直接返回失败结果，不带先前成功列表。因此本计划预检成单次请求，不把隐式分段当可靠交付账本。[官方 Channel API](https://github.com/larksuite/channel-sdk-python/blob/main/docs/reference.md)。
- `Runner.run_streamed` / `FeishuChannel.stream` 存在公开能力，当前 Vertex `stream_response` 明确拒绝；本计划不启动流式。[官方运行文档](https://developers.openai.com/api/docs/guides/agents/running-agents)。

没有构造真实 channel、调用模型/平台、读取凭据或触碰公司服务；这些核查不能证明 native 客户端排版、平台容量、文件上传或业务任务质量。

### 1.2 关键调用链与文件责任

正式入口可信身份/目标 → 单 Agent Runner → function/MCP tool → GovernedTools → Adapter/官方 MCP → 四种投影与 Evidence → 最终回答与 Session 提交 → ResultDelivery 当前授权复核 → 完整交付构造与请求绑定 → 官方 SDK 上传/发送 → 分项回执持久化 → 整体投递状态。任何发送失败都停留在已保存结果之后，不逆向调用 Agent 或 Adapter。

| 责任 | 预计涉及文件，实施时只改实际受影响者 |
| --- | --- |
| 事实集合、完整性与最终数据契约 | `src/xiaowei/models.py`、`evidence.py`、`app.py`，必要来源元数据在既有 Adapter/工具中补齐 |
| 保真文件、分文件及自然排版 | `feishu_report.py`、`feishu_render.py`、`feishu_markdown.py` |
| 请求绑定、回执、重发及恢复 | `channel.py`、`channel_store.py`、`feishu.py`、`cli.py`、`storage.py`，应用表迁移使用实施时下一未占用版本 |
| 监控采样专项 | `runtime.py`、`mcp.py`，现有 Prometheus 协议用例与事实投影；不增加直连源作为偷偷 fallback |
| 文档事实 | ARCHITECTURE 维护契约；本计划维护任务；handoff 维护验证/缺口；实现可用后才更新 README/运维操作 |

## 2. Agent 任务、信息与工作方法

### 2.1 沿用业务样例

复用 `tests/sdk_core/dialogue_gate.py` 的 desc_qualified、desc_ambiguous_targets、yesterday_known/yesterday_missing、sql_only、raw_*、ddl_history_and_refresh、pages_with_empty_middle、r7_*；诊断沿用 `gate0.py::DIAGNOSIS_SAMPLES`。监控续查/可修正 PromQL 沿用 `test_monitoring_read_recovery.py`、`test_prometheus_repair.py`、`test_grafana_g3.py`、`test_feishu_reports.py` 的任务。新增的交付表现只把本会话确认的 1000 行/20 行预览、24 面板看板摘要、HTML/MD 可读性和部分投递失败加入这些任务，不建设评测平台。

| 业务问题 | Agent 在批准范围内决定 | 可获得的信息与必须澄清项 | 成功标准 |
| --- | --- | --- | --- |
| 找到 Node Exporter 看板并说明内容 | 在已授权 Grafana 源查找/读取已知 UID；是否补面板/变量信息 | UID、24 个面板、描述来自工具；名称多义或目标不清且无可信唯一匹配时澄清 | 简洁指出配置摘要；完整获准摘要可查；不冒充实测或完整 dashboard JSON |
| 查指定集群昨天订单并解释 | 写受治理 SQL；根据问题补合法聚合/结构证据；选何时结束 | 可信集群/业务口径/时区及结构可复用；缺时间字段、时区或业务定义时问用户 | 实际 SQL、完整已返回获准行和聚合口径一致；1000 是返回数，正文预览准确 |
| 给表结构/原始 DDL，追问最新定义 | 按需调用 describe/layout/show_create_table；可信旧证据可引用，明确“最新”才重新取证 | 准确目标/库/表与对象类型；多候选由用户选择，不能替换失败 DDL | 原文保真；分页可继续；查询范围或原值受限清楚，不用模型摘要代替原文 |
| 解释 SQL，不执行；输出 HTML/MD | 不调用业务查询；必要时用获准元数据/显式 EXPLAIN LOGICAL，或仅解释 | 用户 SQL 与诊断 Tool Scope；目标不明且工具所需时澄清 | 无原查询执行；无事实说明标为建议；文件生成不授予执行权限 |
| 指定主机/时间窗巡检并给趋势 | 选择获准监控源、合适选择器/聚合/步长，补缺证据，预算内结束调查 | 真实实例、变量/宏、源对应、时间范围；单位/阈值未知则补证或问，不能猜生产源 | 所需范围有真实序列；来源失败被指出；图表与完整样本同口径，结论限于已有证据 |

文档格式和消息容量由代码处理。Agent 不为了让消息变短少取证、不重跑已有成功查询来制造更小结果、不因要求 HTML 扩大工具范围。主体仍自主选工具与调查顺序；上述顺序仅示意。必要依赖是可信目标/授权 → I/O 前治理；变量/源对应未确认 → 不执行该面板指标；写动作仍依赖已送达审批材料、授权批准与 Action 复核。

### 2.2 修正、继续与停止

| 条件 | 按已批准契约处理 | 用户看到的内容 |
| --- | --- | --- |
| I/O 前可修正参数/受支持 SQL 写法拒绝 | 同一 Runner 在现有预算内改正、选择获准工具或澄清；不重发相同错误参数 | 可操作原因与下一步，拒绝不伪装执行失败 |
| 可修正 PromQL 错误 | 沿已批准分类，只对获准可修正类继续；不新增统一重试次数 | 修改原因与实际取得结果；继续失败时限制说明 |
| 已批准的监控只读来源故障 | 复用已有分类续查独立获准源，不自动跨模型/未授权源 fallback；本轮保存失败来源 | 未核实来源保留主消息，不能给整体健康结论 |
| 普通数据库 I/O 后超时/失败、写结果未知 | 沿当前工具失败分类停止相关操作或整轮；不自行扩大为新的数据库重试/降级契约 | 如实说明失败/未知；不能用别的事实冒充该次结果 |
| 目标/变量/时间/业务口径缺少不可替代信息 | 暂停依赖该信息的操作并问用户；可完成独立且获准的部分 | 问清必要信息，说明目前范围 |
| 工具/轮次/期限预算用完 | 不新增 I/O；仅按已有一次无工具收尾等机制校验并结束，未通过就失败 | 已取得事实与未覆盖项，或安全失败回执 |
| 权限撤销、Evidence 不真实/过期、无法复核 | 阻断相关事实交付；不能移除依赖引用来绕过；临时不可复核保留已保存结果 | 未能交付及是否可稍后重发，不披露被限制数据 |
| 渲染/容量/上传/发送出错 | 进入交付恢复，零新增模型/业务工具 I/O；未知按设计来源的状态规则停止 | 未完整交付，已发送部分与可恢复方式；不能说整轮已送达 |

### 2.3 工具说明的必要调整

沿用已有工具名称、参数模型与真实 schema；只有影响任务选择/结果理解的说明才改。每项说明包含适用问题、必填目标与输入、结果范围/单位或缺失、可修正错误及停止条件：

- list/describe/layout/show_create_table：发现、字段、布局、原始 DDL 分清；准确对象不必先搜表；游标、类型限制与 DDL 失败不替代规则沿用。
- run_readonly_query / list_slow_queries：明确用户 SQL 查询与快捷诊断的不同口径；复用 R4 遗漏计数；禁止把 max_rows 当数据库总数或导出 INTO OUTFILE。
- Grafana summary/panel queries：配置摘要、变量/宏和数据源对应不等于指标执行；panelId 的已有语义不改；无法取得单位/阈值必须明说。
- Prometheus query/range_query：授权源、选择器、实际起止、step、warnings、未知/截断范围；修正 PromQL 按现有分类，不强制先发现标签或读规则。

不新增“导出工具”要求模型自行调用；交付是经过验证的最终回答之后的代码行为。文字提示的改善以任务样例验证，不替代权限、SQLGuard 或 Evidence 校验。

## 3. 跨模块接口与审查重点

字段语义、状态及边界以设计来源为准，普通函数名/模块拆分留给开发；实施前必须确定以下消费者一致：

| 边界 | 必须落实的接口约束 |
| --- | --- |
| AgentAnswer → 验证器 | 保留现有 facts 引用/inferences/advice/clarification/report_format；若要增加标题等字段须证明直接消费者并验证真 SDK/provider schema，不能顺便建布局 DSL |
| Evidence → 交付构造 | 完整原值和可信来源/投影范围独立于聊天预览；旧未知原因可保留未知；复用成功记录识别本轮结果，不展示未授权字段 |
| 交付构造 → 请求存储 | 一次绑定全部必需部分、范围与摘要、回复目的地及语义版本；第一次 I/O 后不可重新规划/替换原文绕过未知状态 |
| LarkTransport → 投递存储 | 保留经类型检查的成功平台 ID；错误区分 sent/failed/unknown；预检单请求不依赖 chunk_ids；success 无 ID 仍未知 |
| requests resend / 恢复 → 发送 | 按语义版本处理，已成功部分不重发；未知部分不自动接管为可发；旧 attempt 无落定/继续发送权；零取数 |
| 存储迁移 → 历史/回退 | 旧 sent 不自动晋级完整；旧原字节/键/unknown 保留；错 schema 拒绝启动；恢复备份后不自动补发 |

**Review Focus（每项由下节指定切片覆盖）：**

1. 新模板/固定说明改变后旧文件的原字节与当前授权绑定是否仍可安全核验（D1a/D2）。
2. 平台成功但回执未落库、取消与失去 owner 的旧协程是否会重复发送或伪造完整（D1a）。
3. brief、非引用调查证据与投影丢失会否造成附件缺字段或把局部数称全量（D1a/D3）。
4. HTML/MD/native Markdown 的不可见控制符、嵌套类型及恶意链接/@ 会否改值或触发平台语义（D2/D3）。
5. 24 个面板、默认时间窗、抽样曲线/缺点会否被模型写成全量实时健康（D3/D4）。

## 4. 可独立验证的切片与依赖

顺序为 D0 → D1a → D1b → D2 → D3；D4 的协议核查可独立进行，正式图表交付依赖 D2/D3。每片独立提交并按风险审查；阻断只暂停依赖该问题的后续片，不靠全仓测试数量推进。D0 是本次文档工作；其余均未授权实施。

### D0：锁版、范围与设计核查

- [x] 核对基线/脏工作、公共 SDK 签名、源协议与共同根因；无网络合成验证见 §1.1。
- [ ] 审阅 ARCHITECTURE C1–C6，确认文件总量/数量初值及未知发送的人工核实边界。
- [ ] 按精确文档提交做独立审查；区分已证实缺陷、待核实疑点和接受的边界；修正文档后交新 SHA。

**退出：** 文档可审阅且影响正确性的设计门槛列清；无产品代码修改。文档检查通过只证明计划/引用一致。

### D1a：单文件加正文的完整交付与分项恢复

**涉及：** models/evidence、feishu_render/report、channel/store、feishu/cli/storage 与下一应用表迁移；测试扩展 `test_feishu_reports.py`、`test_channel_service.py`、`test_channel_store.py`、`test_evidence.py`、`test_r3_render.py`、群 Action/恢复相关用例。

- [ ] 先在旧实现复现：1000 行只预览20行而无完整文件；brief 文件缺原文；文件成功正文失败无法只补正文；容量回执被当作完整。断言零重查询。
- [ ] 以现有安全文件模板先实现一份有界完整文件加正文、可信完整性与本轮完整结果集合；未显式要求文件的普通短结果仍单条无附件。加入两项对照验收：同一短结果未要求文件时零上传；显式要 HTML/MD 时按选定格式生成文件并确认文件和正文均送达。单文件仍放不下时明确未完成，等 D1b，不退裁剪卡片冒充完成。
- [ ] 完成原子分项绑定、平台回执、版本化整体 sent 语义、上传/发送 owner 和当前权限检查；CLI 按已确认的缺失部分补发。
- [ ] 注入上传超时、平台成功落盘前崩溃、消息ID缺失、文件已发正文失败、取消/重启/旧attempt、撤权/过期/成员离开、存储失败；断言已成功部分零重发，未知零盲发，绑定失败零上传，撤权后零后续外发。
- [ ] 验证旧单文件/旧 sent/旧 unknown/无context旧回答与新记录；模板变化不重写已绑定文件；暂停审批材料完整送达与重复批准零写回归。

**退出：** 真实 SDK + 实际测试 PostgreSQL + 合成平台证明完整单文件及状态恢复；schema 升级/备份隔离恢复验证；审查精确 SHA。真实飞书/模型仍单独待验。

### D1b：有界分文件

**依赖：** D1a 分项状态；C5 初值经 D0 审阅且有合成容量证据。涉及 feishu_report/发送构造与同一投递存储；复用 D1a 测试，不建新的任务队列。

- [ ] 用完整序列化文件边界、400,000 字节/10,000 行等值与超一、宽表、长 SQL/字段、整章不可分、总量/文件数等值与超一验证候选设计；测量完整交付准备、逐项验权/依赖探测、存储、上传与发送的耗时和投递权占用，以及字节与峰值内存，不能只统计原 JSON 或用上传/消息发送次数代替总等待上界。
- [ ] 实现数据集/整行/整章拆分及自身范围/`1/N`；全部文件在首次上传前构造并检查完，失败时零上传；附件与正文使用同一获准数据绑定。
- [ ] 验证第二文件明确失败/未知、发送中撤权、最后正文失败、进程重启；结果只补确定缺失部分，整体不误记 sent，不取数。

**退出：** 大结果已取得部分完整覆盖、无重行漏行/字段，所有文件各自合容量，总预算不被拆分绕过；部分投递可恢复。阈值不满足代表任务则修订 C5，不静默放宽。

### D2：通用可读 HTML/MD

**依赖：** D1a/D1b 的完整原值与绑定；涉及 feishu_report、必要的 models/evidence/app 校验；复用 `test_feishu_reports.py`、`test_feishu_report_browser.py`、`test_dialogue_contract.py`。

- [ ] 用查询表格、结构/DDL、SQL诊断、看板摘要、无事实解释、恶意字段和长分析作为渲染样例；证明完整性与类型，不把“好看”代替这些断言。
- [ ] 在既有模块实现自然章节、表格、代码、嵌套值的保真展示；HTML 加固定样式与相配 CSP，MD 用阅读表格/段落及安全围栏。按形态复用，不固定巡检标题，不新增任意模型 HTML 或主题平台。
- [ ] 允许经完整验证的无事实 advice/clarification 文档；曾经成功取数但遗漏引用仍拒收；缺事实不写已巡检/健康。
- [ ] 浏览器从本地文件正式打开：窄屏/桌面、长表、换行/控制符、大整数与NULL、嵌套对象和普通字符串区分；复制 SQL/DDL 对照获准原文；恶意 HTML/@/URL 不加载资源，零外部请求。
- [ ] 旧文件重发保持原字节和版本绑定，渲染版本变化不改写历史；达到新容量时分文件仍遵守 D1b。

**退出：** 数据/分析/限制完整，文件实际可读、离线无外部依赖，类型与原文复制边界明确；真实用户附件打开另验。模型 schema/指令改动按 §5 固定任务集验收。

### D3：自然主回复与按需依据

**依赖：** D2 通用文档；涉及 feishu_render/markdown、LarkTransport、必要的 app 指令与 ToolPolicy 说明；测试复用 `test_feishu_render.py`、`test_feishu_format.py`、`test_r3_render.py`、`test_session_warning_render.py`、`test_dialogue_contract.py` 与实际 SDK 协议用例。

- [ ] 复现本会话看板样例的固定技术说明/原字段占主消息及截断；期望短问题直接回答、无不必要附件，完整依据在同条折叠区；用户问实时健康时不得只有看板摘要结论。
- [ ] 普通说明用官方受控 Markdown；数据库原值/实际SQL使用明确组件。沿现有 markdown-it 约束禁止主动链接、媒体、@与HTML，核对 native/structured 转换后的最终请求体、组件及复制，不靠 SDK 自动分段。
- [ ] 按可信说明语义把技术详情放查看依据、业务限制留主消息；未分类限制默认保留。原文/表结构请求把对应事实当业务结果，长调查配短答案不能把整段分析挤掉。
- [ ] 验证多字节/双重转义、超组件、边界容量、无事实长回答、partial来源/分页、会话系统提示、生产写审批材料及原群消息关联；全部需要的部分仍经过 D1 状态链。
- [ ] 用 §2 样例跑离线契约及获准真实模型旧/新对照，记录工具选择、关键事实、未覆盖表达、步数/时间/用量；不比较逐字答案。真实 Lark 手机/桌面验收 Markdown、折叠/表格和复制。

**退出：** 任务质量与治理各自达标；短结果无附件、大结果标明预览并完整交付；模型/客户端缺环境时保留门槛，不继承旧 R7/E6 的豁免。

### D4：监控真实采样与静态曲线专项

**前置：** 下列协议疑点须先关闭；读取授权/治理保持当前机制。涉及 runtime/mcp/必要 Evidence结构与 feishu_report；复用 `test_prometheus_mcp_protocol.py`、`test_prometheus_p2_protocol.py`、`test_prometheus_repair.py`、既有报告浏览器用例。

- [ ] 先核对官方 Prometheus MCP v0.18.0 的实际返回和完整性标记：是否有结构化序列；文本协议的时间戳/值/标签是否可完整且无歧义还原。只用已校验二进制+loopback合成Prometheus，记录精确版本/SHA、真实SDK响应；不能从截图、起点/step猜采样点。
- [ ] 可可靠读取时补最小严格结果契约；逐点保留网格/缺点/非有限值/采样精度和来源，继续四种投影限制；不能可靠解析时停止图表实现，列明所需上游契约修订供审阅。
- [ ] 实现自有静态SVG：轴、单位/未知单位、真实时间范围、图例及明确范围的统计；抽样可视预览须标注且完整获准采样仍可得。Grafana 默认窗口不能替代实际 Prometheus 执行窗口。
- [ ] 覆盖多序列、错序/重复点、空结果、缺点/NaN、warnings/截断、部分源失败、单位未知/主机对应未确认、过多数据超预算；代码统计不使用预览代替完整范围，未知健康不伪装绿色评分。
- [ ] 核对新增字段的 policy/data_scope 指纹、旧字符串证据/会话的失效影响和迁移说明；在获准真实源和模型上用同一主机同一窗口对照。

**退出：** 精确源协议到Evidence到最终图形可追溯；专项离线与真实模型/工具/客户端分别留证。不因通用HTML通过就宣布监控曲线可用。

## 5. 环境、检查与兼容恢复

### 当前文档检查与实施检查分开

文档阶段只做现有文档事实检查、相对链接/anchor与差异检查，以及 §1.1 无网络 SDK 核查。旧环境缺 Pillow 等依赖时如实记录，不安装到用户运行环境或假称整套测试通过。

实施前在该切片独立环境按当前锁文件准备；以下是**计划命令**，本次没有运行产品回归：

```bash
uv sync --locked --extra dev
uv run --locked python -m pytest tests/sdk_core/test_feishu_render.py tests/sdk_core/test_feishu_reports.py tests/sdk_core/test_channel_service.py tests/sdk_core/test_channel_store.py -q
uv run --locked python -m pytest tests/security/test_docs_command_consistency.py -q
```

D2/D3 增加其上节列明的受影响用例；D4 增加真实锁版 MCP 协议。按实际改动运行 `uv run --locked ruff check <受影响路径>`、`uv run --locked mypy <受影响新包模块>` 与 `git diff --check`；尖括号是待替换的路径占位，不是可直接运行命令，不扩成旧包全仓验证。

需要存储的用例使用仓库 `compose.sdk-test.yml`/`postgres_harness.py` 声明的隔离测试 PostgreSQL与 `SDK_TEST_POSTGRES_URL`，只放行loopback；浏览器按现有 `-m browser`、`SDK_TEST_CHROME` 运行本地正式入口/本地文件，不占用已被用户进程监听的端口。迁移扩展现有 storage 测试，备份/恢复采用仓库已批准流程。真实模型、飞书、公司源及部署须另获授权；准备固定合成样例不代表真实环境通过。

### 升级与恢复

- 实施迁移前核对主线应用表版本，在事务内增加必要分项与语义版本；不要把新元数据写入 SDK Session 表。
- 旧请求/报告不重跑、不改字节、不自动续发；新程序按旧契约标识读取，错误密钥/错误schema仍在 I/O 前拒绝。旧模板核验与当前权限冲突不能靠减少 Evidence 范围绕过。
- 旧二进制不认识新 schema 时沿现有拒绝机制；回退用旧镜像、旧配置、迁移前备份恢复的隔离数据库，不原库降级；飞书已收到的副本无法回退。
- 重启恢复先确认 owner 已失活、作废旧attempt，未落定发送标未知，不自动上传/发送；恢复后操作者仅补确定未发部分。CLI 原参数优先复用，是否需要可安全查询的分项状态由D1a接口审查确定，不新增管理平台。

## 6. 尚未关闭且影响正确性的疑点

| 疑点 | 关闭方法与阻断范围 |
| --- | --- |
| native/structured 的真实序列化、客户端容量与复制保真 | D3 用锁版SDK+合成协议测真实post请求；真实Lark两端补验。未验证的 native 混合组件不得替换已保真路径 |
| 平台成功未落库或无ID未知发送如何人工核实 | 核对公开平台查询与去重窗口、保存稳定关联标识；无可靠证据继续未知且禁盲发。D1a须明确可恢复与不可恢复分支，不能声称 exactly-once |
| 旧文件只存原字节/digest，缺少足够的内容/Evidence绑定 | 逐条核对旧请求已有回答/上下文与报告记录；可证明才兼容，不能证明则受控拒绝。D1a 的历史兼容审查门槛 |
| 来源截断与投影省略原因在旧标记中可能混合 | 复用已有来源/投影与R4计数；无法证明为未知，新增元数据范围最小且说明指纹影响。不阻断可准确交付的已保存部分，但阻断虚构“完整取数”文案 |
| C5 文件总量/数量初值是否覆盖代表任务 | D1b前按合成完整HTML/MD实际编码与内存/耗时测量，并由用户审阅C5。未确认前不把初值写成已批准限制 |
| 官方Prometheus结果能否可靠恢复逐点结构、精度与缺失 | D4先做锁版真实SDK/Server协议核查；不可靠则暂停图表、提出最小上游协议修订，不换源或凭模型补齐 |

以上缺口只阻断依赖它的实现/验收。流式、Web下载和新输出格式不用于解决当前缺口，保持后置。

## 7. 交付与审查记录

本次文档提交只包含本计划、ARCHITECTURE 对应修订契约、handoff/路线入口及旧计划/README的状态指向。普通文档修订不强制多轮评审；本设计涉及分项送达、未知重发和数据绑定，安排一次针对精确文档SHA的独立审查。自行复查不称独立审查，离线协议核查不称新功能通过。

每片交付写明精确SHA、真实差异、实际检查及结果、代码/离线/模型/工具/渠道/部署/用户验收层级、接受的边界和下一项。有阻断性安全/恢复问题时暂停依赖片；检查通过且没有新修改/疑点时不重复扩大全仓检查。
