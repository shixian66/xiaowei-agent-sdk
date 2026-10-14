# 自然回答与完整结果交付修复实施计划

> 文档版本：v1.2，2026-10-14，Asia/Shanghai。源码基线 `0c54442fbd15d92e2d9fc544b6e7cddd8cc0cbca`（本地 `origin/main`，含 PR #99）；审查版本为包含本文件的精确 Git 提交，不以源码基线代替。
> **执行说明：** 当前只授权设计、计划、必要文档及有边界的离线核查；产品实现、真实服务调用、推送、合并、部署未授权。实施另获授权后，默认串行交付一个可验证切片，使用 `superpowers:executing-plans`；仅用户选择后采用子代理实施。

**目标：** 普通回复简洁自然；模型引用及既有代码强制引用的获准结果、已保存分析完整可得；其他成功调查只提供简短依据索引；HTML/MD 在同类场景共用可读展示；部分投递可定位和手动恢复。

**架构：** 保留单 Agent、真 Runner、output_type、治理、Evidence、Session 和渠道 SDK。扩展现有投影、文件与请求投递链；完整交付为共同基础，文档和自然消息可分别改善，监控结构化曲线单独验证。

**技术栈：** uv.lock 的 OpenAI Agents SDK 0.22.3、lark-channel-sdk 1.4.0、markdown-it-py 4.2.0，现有 PostgreSQL/SQLAlchemy、Python 标准库；基线应用表 v11。不计划新增运行依赖。

**唯一设计来源：** [ARCHITECTURE §7 自然回答与完整结果交付](../../../ARCHITECTURE.md#result-delivery)维护产品取舍、交付集合、数据/权限、容量与状态契约；本文件只维护源码核查、实施任务、依赖、验收和疑点。[AGENTS](../../../AGENTS.md)维护协作/验证规则；工具与查询边界直接引用 ARCHITECTURE §5–§6。

## 1. 已核实根因与最小方案

| 当前调用点 | 已核实事实 | 复用与必要调整 |
| --- | --- | --- |
| app → Runner → AgentAnswer | 最终回答强制引用只覆盖 `Application._queries`：查询减诊断/discovery，加既有 propose/approval，减 write；漏引拒收 | 保留集合与拒收；不增加意图分类器，也不强制所有诊断/发现原值 |
| governance/MCP/工具 → Evidence | 先治理后 I/O，四种用途投影；原始结果不长期保存 | 完整性使用可信来源/投影标记及既有计数，不能恢复已丢弃数据 |
| Evidence → DeliveryFact | 当前只有引用证据生成展示事实；未引用 context 仍用于权限复核；result_json 保留渠道获准结构，rows 已显示化，brief 是历史摘要 | 文件从获准原结构生成；辅助索引单独只列可信工具/对象/范围元数据 |
| feishu_render | 本地请求预算 30,000 字节；行和分析可裁剪，容量说明也可能落 sent | 先完整构造和测量再选载体；不把裁剪或固定失败说明当完整结果 |
| feishu_report | 只接受有 facts 的只读报告，固定巡检标题；HTML pre/MD 围栏；单文件 400,000 字节/10,000 行；brief 原文省略 | 先复用现有安全模板完成交付，再改善表格/章节/代码；不建模板注册平台 |
| send_delivery/ResultDelivery/channel_store | 文件有原字节/键绑定，transport 丢弃成功平台 ID；整轮无分项账本 | 扩展当前存储的必需部分、回执与语义版本，复用 owner/attempt/防重；整体状态派生 |
| app._paused → requires_complete → claim_native_action | SDK 暂停先于普通最终引用检查；暂停回答只有审批材料引用；整轮禁止文件；原暂停请求 delivery=sent 才能批准 | C7 必须补暂停数据保存/构造及混合交付，不能只拆发送条件；复用原材料组件和批准门槛 |
| runtime/Prometheus MCP | range_query 已登记 result 是字符串；Grafana summary 是配置摘要；R4 已有慢查询遗漏原因 | 图表先验证锁版协议的采样结构，R4 计数直接复用 |

仅加 CSS 无法解决裁剪、brief 原值和部分投递；新增文档平台没有直接消费者。扩展共同根因所在的现有链，普通内部函数、辅助模块与签名留给开发决定。

### 1.1 SDK 公开能力与离线证据

核查使用原仓库已有 `.venv` 中的同版本安装包，并以本计划工作树的 src 为 PYTHONPATH；不是本工作树重新安装的环境。用于锁版签名/源码和无网络合成验证，不能证明产品回归通过：

- 公开 `Agent(output_type=AgentAnswer)`、`AgentOutputSchema.validate_json` 可保留自然段落的结构化回答；SDK 验证类型，不负责证据真实性或完整送达。[官方 Agent 定义](https://developers.openai.com/api/docs/guides/agents/define-agents)。
- `FeishuChannel.send(to, message, opts=None)`、`upload_media(source, *, kind, file_name, file_type)` 和已上传 file_key 可复用；公开 `SendOpts.uuid` 可传稳定标识；SendResult 有 success/message_id/error/raw/chunk_ids。[官方 Channel API](https://github.com/larksuite/channel-sdk-python/blob/main/docs/reference.md)。
- 锁版 sender 对首段透传 opts.uuid，后续隐式分段自行生成 uuid；多段失败不回报之前成功的全列表。平铺回复只有首段关联原消息。本计划每个部分预检为一段，分项记录由小维负责。
- 锁版 sender 遇 post 格式错误会降级纯文本再发送，即使 max_attempts=1 也不能据此声称只有一次 HTTP。SendResult 成功本身不证明排版/原文保真；D3 必须核对降级内容，无法证明原文保真时沿用明确卡片/文件组件。
- 公开 `markdown_to_post_ast(..., tag_md_mode='native'|'structured')` 均有转换结果；合成 SQL 在 structured code_block 中逐字相等，native 的客户端表现/复制未实测。[官方 Markdown 文档](https://github.com/larksuite/channel-sdk-python/blob/main/docs/markdown.md)。
- `Runner.run_streamed`/`FeishuChannel.stream` 有公开能力，当前 Vertex stream_response 明确拒绝；流式后置。[官方运行文档](https://developers.openai.com/api/docs/guides/agents/running-agents)。

uuid 的真实去重窗口、重复时返回原消息 ID 还是报错均待获准真实飞书验证。合成平台只验透传、复用与状态，不能证明平台去重。没有调用模型、真实 channel/公司服务或读取凭据。

示例配置 16 次工具调用 × 400,000 字节渠道投影 = 6,400,000 字节理论上界，尚未计历史、分析和排版。合成容量探针中 390,014 字节 JSON 经 HTML 转义变为 1,950,034 字节；这仅证明膨胀不可忽略，不是生产分布或通用倍率。C5 推导在 D1b 测量，不把固定 3.2MB/8文件沿用为已批准限额。

### 1.2 关键调用链与责任

可信身份/目标 → 单 Agent Runner → function/MCP → GovernedTools → Adapter/官方 MCP → 四种投影/Evidence → 经验证最终回答（或 SDK 暂停材料与必要数据）→ 保存结果 → ResultDelivery 当前授权复核 → 完整构造/绑定 → 官方 SDK 上传/发送 → 分项回执 → 派生整体状态。恢复从已保存结果开始，不逆向运行 Agent 或业务查询。

| 责任 | 实施时只改实际受影响文件 |
| --- | --- |
| 引用集合、辅助索引、完整性与暂停数据绑定 | models、evidence、app，必要来源元数据在现有工具/Adapter 补齐 |
| 文件、拆分与自然排版 | feishu_report、feishu_render、feishu_markdown |
| 分项存储、回执、重发及恢复 | channel、channel_store、feishu、cli、storage；迁移采用实施时下一未占用版本 |
| 监控采样专项 | runtime、mcp 与现有协议/投影；不增加直连 fallback |
| 文档事实 | ARCHITECTURE 契约、本计划任务、handoff 验证/缺口；实现可用后才更新 README 操作方法 |

## 2. Agent 任务、信息与工作方法

### 2.1 沿用业务样例

复用 `tests/sdk_core/dialogue_gate.py` 的 desc_qualified、desc_ambiguous_targets、yesterday_known/yesterday_missing、sql_only、raw_*、ddl_history_and_refresh、pages_with_empty_middle、r7_*；诊断沿用 `gate0.py::DIAGNOSIS_SAMPLES`。监控沿用 test_monitoring_read_recovery、test_prometheus_repair、test_grafana_g3、test_feishu_reports 的任务。只向既有任务补本会话的展示/恢复期望，不建设评测平台。

| 业务问题 | Agent 在批准范围内决定 | 信息与必须澄清项 | 任务成功标准 |
| --- | --- | --- | --- |
| 找 Node Exporter 看板并说明内容 | 选择获准 Grafana 源；读取 UID，按需补面板/变量 | 工具给出 UID/24 面板/描述；目标多义且无可信唯一匹配才问 | 引用配置摘要，直接说明内容；不冒充实测或完整 dashboard JSON |
| 查指定集群昨天订单并解释 | 写受治理 SQL，按需补结构和合法聚合，选择结束调查 | 复用可信集群/时区/业务口径；缺时间字段或必要业务定义才问 | 引用实际 SQL、获准行和聚合；1000 是返回数；预览准确、完整选定结果可得 |
| 给表结构/原始 DDL，追问最新定义 | 按需 describe/layout/show_create_table；旧证据可复用，“最新”才重新取证 | 准确集群/库/表/对象类型；多候选需选择 | 必须引用所需原文而非仅转述；漏引属于模型任务失败，不声称代码必能拦住 |
| 解释 SQL，不执行；输出 HTML/MD | 不执行业务查询；按现有许可补元数据/显式 EXPLAIN LOGICAL 或仅解释 | 用户 SQL、目标和 Tool Scope；只有依赖工具时才问必要目标 | 零原查询执行；说明标为建议；格式要求不授予执行权限 |
| 指定主机/窗口巡检并给趋势 | 选获准源、选择器/聚合/步长，补证并在预算内结束 | 实例/变量/源对应/实际窗口；单位/阈值可补证，必要缺口问用户 | 图形对应真实序列；结论限已有范围，指出失败源/缺点 |
| 查大结果并提出已批准的监控动作 | 自主选查询/提出工具，SDK 按现有授权暂停等待批准 | 保留可信 Action 材料、请求/数据范围与期限；批准身份来自可信入口 | 数据、正文、材料完整交付；全部确认才可批准；恢复不重查、不重写 |

正文格式和容量由代码处理。Agent 不为消息变短少取证、不重跑成功查询制造小结果、不因 HTML 扩大范围。上述工具顺序仅示意；必要依赖是可信目标/授权 → I/O 前治理，变量/源对应确认 → 相应指标查询，完整审批材料送达和有效批准 → Action 复核/写入。

### 2.2 修正、继续与停止

| 条件 | 沿已批准契约处理 | 用户看到的内容 |
| --- | --- | --- |
| I/O 前可修正参数/支持的 SQL 写法拒绝 | 同一 Runner 在现有预算内改正、换获准工具或澄清；不重复相同错误 | 可操作原因，不伪装已执行 |
| 可修正 PromQL 错误 | 仅沿已有分类继续，不新增统一重试次数 | 实际取得的范围；持续失败则说明限制 |
| 已批准监控只读源故障 | 按已有分类继续独立获准源，记录失败；不跨模型/未授权源 fallback | 未核实来源留主消息，不给整体健康结论 |
| 普通数据库 I/O 后失败/超时，写结果未知 | 沿现有分类停止相关操作或整轮，不顺带新增重试/降级 | 如实说明失败/未知 |
| 缺不可替代的目标/时间/业务信息 | 暂停依赖操作并问清，可做独立获准部分 | 必要问题及当前范围 |
| 预算用完 | 不新增 I/O，仅沿已有一次无工具收尾等机制验证结束 | 已取得事实/未覆盖，或安全失败回执 |
| 撤权、Evidence 不真实/过期/不可复核 | 阻断相关交付，不删依赖引用绕过；临时故障保留已保存结果 | 未能交付及恢复条件，不披露受限数据 |
| 渲染/容量/上传/发送失败或未知 | 转入 C6 交付恢复，零新增模型/业务查询；权限复核可有元数据 I/O | 未完整交付、已知部分和手动恢复方式 |

### 2.3 工具说明的必要调整

只改影响任务选择/结果理解的说明，沿用名称、参数和真实 schema。说明写明适用问题、必要输入、结果限制及可修正错误：

- list/describe/layout/show_create_table：发现、字段、布局、DDL 分清；准确对象不必先搜表；游标/类型和失败不替代规则沿用；用户要原文须引用相应证据。
- run_readonly_query/list_slow_queries：查询和快捷诊断口径分清，R4 遗漏计数复用；max_rows 不是总数，不导出 INTO OUTFILE。
- Grafana summary/panel queries：配置/变量/宏/数据源对应不等于指标执行，沿用 panelId 语义，单位/阈值缺失明说。
- Prometheus query/range_query：授权源、选择器、真实起止/step/warnings/截断范围；按现有可修正分类继续，不强制先查标签或规则。

不增加模型“导出工具”，不新增用途前置分类器。模型只写分析，不写附件已送达等平台状态；代码生成交付说明。指令/说明改动须做真实模型任务验收，不能替代权限或 Evidence 校验。

## 3. 跨模块接口与审查重点

详细语义以 ARCHITECTURE 为准，开发前必须保证消费者一致；内部函数名不构成设计门槛。

| 边界 | 必须落实的约束 |
| --- | --- |
| AgentAnswer → 验证 | 保留引用/inferences/advice/clarification/report_format；强制引用遗漏仍拒收；新增 schema 字段须有直接消费者并验证真 SDK/provider，不建布局 DSL |
| Evidence → 构造 | 引用集合与原值独立于预览；其他成功证据只输出可信元数据索引；区分 SDK 任务、暂停续跑和历史回放，不能用新渠道 turn_id 猜本轮 |
| SDK 暂停 → 保存结果 | 既有强制集合的数据与原审批材料同批保存/绑定，复用成功记录/checkpoint；不导出 RunState，不增加模型循环或未引用辅助原值 |
| 构造 → 存储 | 首次 I/O 前绑定全部必需部分、不可变内容/范围/目的地/语义版本；稳定消息 uuid/首次发送时间持久化 |
| Transport → 存储 | 保留经类型校验的平台 ID；success 无 ID 仍未知；分项聚合整体，现有 parent delivery 只作同事务派生缓存 |
| requests resend → 发送 | 按 C6 手动恢复；成功部分不重发，未知消息不自动接管；旧 attempt 不得继续落定；上传未知不重传 |
| 迁移/清理/回退 | 原字节/键/unknown 保留；旧 sent 不晋级完整；分项字节沿请求过期/级联删除，恢复备份不自动补发 |

审查重点：原字节与权限绑定、旧协程/回执丢失、辅助索引越界、历史 brief、混合审批、Markdown 降级及原值类型、监控范围。分项状态和任务质量分别验收，平台回执不能证明 Agent 已回答正确。

## 4. 可独立验证的切片与依赖

D0 → D1a 为共同基础。D1b、D2、D3 可分别推进：D2/D3 的单文件或短消息只依赖 D1a，涉及多文件才依赖 D1b；D3 不必等待 D2。D4 协议核查独立，最终图形依赖通用文件/完整交付能力，不以 D3 排版为固定前置。实施默认串行一个单元，技术依赖不额外排成固定链。阻断只暂停依赖片；除 D0 外均未授权实施。

### D0：范围与设计核查

- [x] 核对基线/脏工作、公共 SDK/源码与根因；离线证据见 §1.1。
- [x] 按用户答复修订 C3 和 C6，撤回 C5 无依据固定初值。
- [ ] 对包含本文件的精确 SHA 复审 C1–C7；C7 混合暂停/发送/批准提案须在涉及 P4 的 D1a 开工前定下。
- [ ] D1b 前提交 C5 可复核推导及数值取舍；不阻断 D1a 单文件闭环。

**退出：** 文档可审阅，接受代价和门槛明确；文档检查只证明引用/事实一致，不构成实施授权。

### D1a：单文件加正文、分项状态和手动恢复

**涉及：** models/evidence/app、feishu_render/report、channel/store、feishu/cli/storage 与下一迁移；复用 test_feishu_reports、test_channel_service、test_channel_store、test_evidence、test_r3_render 及群 Action/恢复测试。

- [ ] 在旧实现复现 1000 行裁剪、brief 文件缺原文、文件成功正文失败、容量说明误记完成；断言恢复不重查。
- [ ] 验收引用集合：漏引强制查询拒收；未引用 describe/DDL/慢查询/看板只留工具/对象/可信范围索引，无原值；引用时完整可得。用查询加长辅助调查证明短答案无多余附件；不得通过模型少引强制结果变短。
- [ ] 当前安全模板先闭环有界完整单文件加正文；普通短结果零上传，显式短 HTML/MD 仍有文件与正文。完整分析也保留。单文件超限明确未完成，不退裁剪卡片冒充完成。
- [ ] 历史 brief 正反验收：只引用可信摘要不自动附件；明确要文件时按当前读取权限展开原值；部分/撤权/缺原值不伪装完整。
- [ ] 首次 I/O 前绑定部分和稳定 uuid，保存上传键/平台 ID，分项聚合 parent；实际 PostgreSQL 验证事务、owner/attempt 与 readiness。
- [ ] 按 C6 覆盖单条/多部分的确定失败和未知手动恢复，同一 uuid 透传/复用；先前未知加本次明确失败仍未知。合成平台不作为真实去重证据；窗口外/未验证窗口/旧无 uuid 的风险提示覆盖。
- [ ] 附件消息未知只在上传成功且 key 已保存时单独恢复；正文未知等待全部附件消息确认；上传未知零重传。已成功部分零重发，零模型/业务查询；权限元数据复核保留。
- [ ] 注入平台成功落库前崩溃、无 ID、超时、取消/重启/旧 attempt、存储故障、撤权/成员离开/过期；绑定失败零上传，撤权后零后续外发，状态不误记 sent。
- [ ] C7 混合轮次从真实 SDK 暂停入口取证：审批材料与强制查询数据均保存，不泄露未引用辅助原值；单卡/文件路径、数据正文/审批卡各阶段成功、失败、未知及恢复，全部确认前零批准写；材料过期/变更停止未发旧命令，重复批准零重放。包括 completed/paused 的显式重发。
- [ ] 兼容旧单文件/sent/unknown/无 context 记录及模板变化；原字节不改写；实际清理请求后验证全部分项字节/键删除、过期不可读。

**退出：** 真 SDK + 实际测试 PostgreSQL + 合成平台证明所选结果、状态与恢复；隔离迁移/备份恢复验证；精确 SHA 独立审查。真实模型任务、平台去重和客户端分别待验，不能宣称 exactly-once。

### D1b：有依据的有界分文件

**依赖：** D1a 分项状态；C5 的配置关联、排版膨胀/打包与资源取舍完成。复用同一构造/存储/测试，不建队列。

- [ ] 按工具上限 × 渠道投影上限、选定历史原值、完整分析/限制及实测最坏排版膨胀推导总量，纳入整行/整章打包效率；配置变更同步预算，不能沿用固定 3.2MB/8文件。
- [ ] 文件数结合真实平台配额与等待预算测量；普通 N 文件加正文为 N upload + N+1 send，混合材料另加一 send。实测权限/依赖探测、存储、渲染、认证/降级额外 I/O、内存和投递权占用，以及同群下一请求阻塞时长；2N+1 不是总 I/O 上界。
- [ ] 覆盖单文件 400,000 字节/10,000 行边界与超一、宽表、长 SQL/字段、不可分章节、总量/文件数边界；超限明确未完成。若拟定限制拒绝原本合法代表任务，按 C5 单列取舍，不让用户反复缩范围。
- [ ] 按数据集/整行/整章拆分，范围/1/N准确，无漏行漏字段/重行；所有文件在首次 I/O 前生成校验，失败零上传。
- [ ] 第二文件失败/未知、发送中撤权、最后正文失败、重启恢复均沿 D1a/C6，全部必需部分确认才 sent。

**退出：** 所选获准结果与分析完整覆盖、每文件合容量、总预算不能靠拆分绕过，部分投递可恢复；资源上界有证据和批准取舍。

### D2：通用可读 HTML/MD

**依赖：** D1a 原值/绑定，多文件使用 D1b。复用 feishu_report 与 test_feishu_reports、test_feishu_report_browser、test_dialogue_contract。

- [ ] 查询表格、结构/DDL、诊断、看板摘要、无事实解释、长分析与恶意字段作样例；证明完整性/类型，不能只验“好看”。
- [ ] 既有模块实现自然章节、真实表格、代码与嵌套值保真；HTML 固定样式/CSP，MD 安全围栏/表格。按形态复用，不固定巡检标题，不引入任意模型 HTML/主题平台。
- [ ] 无事实 advice/clarification 可成文档，强制结果漏引仍拒收；未引用辅助调查不因此补入原值；缺实测不写健康。
- [ ] 本地正式打开，检查窄屏/桌面、长表/控制符、NULL/大整数/嵌套类型；复制 SQL/DDL 对照原文；恶意 HTML/@/URL 无外部请求。
- [ ] 旧文件重发保持字节/绑定，渲染升级不重写历史；模型 schema/指令调整用 §2 固定任务集验收。

**退出：** 文件完整、可读、离线，类型与复制边界明确；真实用户打开另验。

### D3：自然主回复与按需依据

**依赖：** D1a；大结果需 D1b，不依赖 D2。复用 feishu_render/markdown、Transport、必要指令/工具说明及 test_feishu_render、test_feishu_format、test_r3_render、test_session_warning_render、test_dialogue_contract 和 SDK 协议用例。

- [ ] 复现看板示例的技术说明/原字段压过答案和截断；短问题直接回答，无不必要附件；技术依据折叠、业务范围/未核实来源留正文。
- [ ] 普通说明用官方受控 Markdown，原值/实际 SQL 用明确组件；沿现有 markdown-it 约束，按转换后的请求和组件测容量，每部分仅一段并关联原消息。
- [ ] 触发锁版 post 错误降级，核对内容/回复关系与请求次数；不能据 SDK success 声称已保真。SQL/DDL 逐字相等或明确不用 post；一般正文降级也须保留任务必需语义，否则不作完整交付。
- [ ] 用户要结构/DDL时引用并主展示对应结果；未引用辅助索引无原值，历史回放不冒充新执行；未知业务限制不默认隐藏；完整分析不被长调查挤掉。
- [ ] 多字节/转义、组件/容量、无事实长回答、部分源/分页、会话提示与审批回归；状态仅代码生成，模型不写“已发附件”。所有必需部分沿 D1a。
- [ ] §2 真实模型旧/新对照：所需 DDL/结构原文引用、看板配置与健康区别、零误执行、失败表达、步数/时间/用量；模型漏引所需诊断事实记任务失败。真实 Lark 手机/桌面验 Markdown/折叠/表格/复制。

**退出：** 任务质量与治理分别达标；短结果无附件，大结果明确预览且完整交付；缺模型/客户端环境保留验收门槛。

### D4：监控真实采样与静态曲线专项

**前置：** 关闭协议疑点；读取治理不变。复用 runtime/mcp/Evidence/feishu_report、test_prometheus_mcp_protocol、test_prometheus_p2_protocol、test_prometheus_repair 和报告浏览器测试。

- [ ] 官方 Prometheus MCP v0.18.0 校验二进制 + loopback 合成 Prometheus + 真 SDK：核对结构/时间戳/值/标签/完整性，记精确版本/SHA，不能凭起点/step 猜点。
- [ ] 可无歧义读取才补最小严格契约，保留网格/缺点/精度/非有限值；否则暂停图表并提出上游契约修订，不换源绕过治理。
- [ ] 自有静态 SVG 展示真实时间、单位/未知单位、图例/统计；可视抽样标明且完整获准采样仍可得，Grafana 默认窗口不作实际执行窗。
- [ ] 多序列/错序/重复点/空结果/NaN/warnings/截断/部分失败/对应未知/超预算；代码统计基于明确获准范围，不从预览推全量健康。
- [ ] 核对 policy/data_scope 指纹及旧字符串证据/会话兼容，获准真实源与模型同主机同窗口对照。

**退出：** 源协议 → Evidence → 图形可追溯，离线/真实模型/工具/客户端各自留证；通用 HTML 通过不证明曲线可用。

## 5. 环境、检查与恢复

文档阶段仅事实、链接/anchor、差异、现有文档检查和无网络 SDK 核查。原环境缺 Pillow 等依赖不安装到用户运行环境，也不宣称产品套件通过。

实施前独立环境按锁文件准备；下列是**计划命令，本次未运行产品回归**：

```bash
uv sync --locked --extra dev
uv run --locked python -m pytest tests/sdk_core/test_feishu_render.py tests/sdk_core/test_feishu_reports.py tests/sdk_core/test_channel_service.py tests/sdk_core/test_channel_store.py -q
uv run --locked python -m pytest tests/security/test_docs_command_consistency.py -q
```

按切片补上节受影响测试，按真实路径做 Ruff/mypy/`git diff --check`，不扩成旧包全仓验证。存储用仓库 compose.sdk-test.yml/postgres_harness.py 与 SDK_TEST_POSTGRES_URL，仅 loopback；浏览器沿 -m browser/SDK_TEST_CHROME、本地正式入口/文件，不占用户端口。真实模型/飞书/公司源/部署另获授权；合成平台不能代替真实去重验证。

- 迁移前核对应用表版本，事务扩展分项/语义版本，不写 SDK Session 表；cleanup 实测级联删除。
- 旧请求/字节不重跑、不改写、不自动续发；兼容须证明绑定，错密钥/schema I/O 前拒绝。
- 旧二进制不认识新 schema 仍拒绝；回退用旧镜像/配置及迁移前备份的隔离库，不原库降级；飞书已收副本无法回退。
- 重启确认 owner 失活、作废旧 attempt，未落定发送标未知；后续仅操作者按 C6 恢复确定缺失/未知消息。优先复用 CLI，不建管理平台。

## 6. 影响正确性的疑点与门槛

| 疑点 | 关闭方法与影响范围 |
| --- | --- |
| 交付集合与强制引用/暂停批次/辅助索引的实现对应 | C3 选择已确认；D1a 验证成功记录、checkpoint、context 和历史区别及当前权限；不能增诊断强制集合或只按渠道 turn_id |
| 混合审批数据如何保存及顺序/可批准性 | C7 提案须在相关 D1a 开工前审定；通过真实 SDK 暂停/存储/Action 门槛，不能仅测 renderer |
| uuid 平台去重窗口/重复响应及回执核实 | 待获准真实飞书验证；D1a 离线只验透传/状态。手动恢复按用户接受的 C6 风险保留，未知不因此晋级；缺证据阻断 exactly-once 声明，不永久封死显式恢复 |
| post 转换/降级、真实容量/客户端复制 | D3 合成协议触发分支与客户端两端实测；未证明保真的 post 不替换已保真原文路径 |
| 旧文件的原字节/digest/Evidence 绑定是否足够 | D1a 逐条核对；可证明才兼容，不足受控拒绝 |
| 旧截断/投影原因混合 | 复用来源标记/R4，不能证明则未知；不妨碍准确交付已保存部分，阻断虚构全量 |
| C5 总量/文件数/耗时与配置关联 | 仅卡 D1b：编码膨胀、打包、平台配额、同群阻塞实测与数值取舍；不沿用无依据固定值 |
| 官方监控文本能否保真恢复采样 | 仅卡 D4 图表：锁版协议验证，不可靠则提最小上游契约修订 |

## 7. 版本与审查记录

v1.1 精确提交 `a56ab643c0ef89cfffca0d514758babb0d3ad6ab`，源码基线 `0c54442`。用户转交的 Claude 只读审查指出 B1 集合、B2 混合轮次和 B3/C6 范围三项开工问题；C5 数值只影响 D1b。审查未运行测试，使用本机同版本 SDK 安装包，不是工作树环境。

v1.2 按用户明确答复修订 C3/C6，按补充意见撤回 C5 固定初值；补 C7 暂停数据/审批门槛提案、清理/历史/降级等验收，并放松 D3 对 D2 的依赖。收到的验收条目 6–12 已纳入对应切片；原贴遗漏的 1–5 仍未收到，不虚称逐条闭合。新版本须按精确 SHA 复审，旧审查结论不覆盖这些差异。

每片记录精确 SHA、实际差异/检查、任务质量与治理结果、代码/离线/模型/工具/渠道/部署/用户验收层级和下一项。普通文档不重复评审；本设计跨数据/审批/恢复边界，实施前须独立审查。自行复查、合成平台和文档检查不能替代独立/真实验收。
