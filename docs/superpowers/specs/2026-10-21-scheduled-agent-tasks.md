# 多机器人通知与定时 Agent 任务设计

> v1.0，2026-10-21，Asia/Shanghai；源码基线 `691804424f8bf3ed04c32f806a291cfafa053266`。本文件是待审技术设计，不是功能已实现或上线声明。本轮只写文档和核验离线契约。

产品及权限边界唯一维护于 [ARCHITECTURE：通知与定时任务](../../../ARCHITECTURE.md#scheduled-agent-tasks)；实施顺序见[同名 A–D 计划](../plans/2026-10-21-scheduled-agent-tasks.md)。本文维护源码依据、跨模块契约、固定验收样例、方案取舍与未决问题，不另建需求平台。

## 1. 推荐方案与范围

在现有进程内增加一个小型到期检查循环，复用 PostgreSQL 保存任务和每次执行记录。到期只负责建立独立执行上下文，业务调查仍交给 `Application.run_turn → Runner.run → GovernedTools → Adapter/MCP → Evidence`。Webhook 发送是同一治理层后的本地 HTTP Adapter。固定提醒不启动模型；Agent 任务的合规最终结果由代码形成通知投影，每次最多自动尝试发送一次。

按 A 即时通知、B 一次性固定提醒、C 独立 Agent 周期任务、D Node 巡检交付。C 不识别 `task_type=node`，不按任务名分派业务函数。D 增加当前用例真正缺少的结构化取数与巡检方法，不增加调度专用 Agent。后续 StarRocks、Grafana 等能力只需逐项核验无人值守策略、数据投影和固定样例，不另建 Loop 或通用能力 DSL。

不做任意 URL POST、Shell/curl、MCP Server、通知管理平台、多厂商框架、完整结果导出、Cron/复杂工作流或自动修复。当前没有主机告警规则，本功能产生的是巡检发现，不能说成已经配置/触发的生产告警。

## 2. 已核实的源码事实

下列位置对应上述精确基线，后续开发需复核实际 HEAD。链接指向源码，不表示可以原样复用全部语义。

| 当前实现 | 可复用与限制 |
| --- | --- |
| [`tools.py:governed_function_tool`](../../../src/xiaowei/tools.py)、[`governance.py:ToolPolicy/allowed_contracts/invoke`](../../../src/xiaowei/governance.py) | 本地工具闭包已经通过身份、目标、参数、预算、执行和 Evidence 链。effect 只有 `read/propose/approval/write`；`write` 默认隐藏，`approval/write` 要本次 ApprovedAction；非 read 调用要指定群。不能把通知标成 read 或只在提示词写“要权限” |
| [`actions.py:ActionBinding/ActionService.tools_for/authorize_approval/approve`](../../../src/xiaowei/actions.py) | 已有规范化材料、SDK `needs_approval`、跨消息暂停/恢复、审批人及申请人复核、原子单次执行和未知状态。绑定要求 propose/write，审批属于原轮次和短有效期；不是长期授权。建议用真实的本地任务管理 Action 激活/变更任务，回读本地保存状态，独立保存批准的任务范围；不是伪造监控 ApprovedAction |
| [`app.py:Application.run_turn/_tools_for/_local_tools`](../../../src/xiaowei/app.py) | 可给可信 RunContext 和一条自包含任务消息发起一次正式 Runner；已有 Session、最终类型、轮次期限和全局并发。需增加窄的无人值守工具筛选与装配，不并发修改共享 Agent.tools。现有 query 用途并不等于“仅自动执行的 read 集合” |
| [`runtime.py:registered_tools/monitoring_catalog/_app_config/StaticAccess/open_runtime/_serve`](../../../src/xiaowei/runtime.py)、[`config.py`](../../../src/xiaowei/config.py) | 复用目录、可信配置、安全引用、客户端生命周期和唯一授权来源。StaticAccess 当前群共享读取范围大致为全部已配置目标；群成员在入口核验，单次 authorize 不重新查目录。需增加独立的通知、任务管理与可自动执行集合，不能假定它已有接收群授权 |
| [`models.py:RunContext`](../../../src/xiaowei/models.py) | 已有可信身份、范围、预算及 Evidence 标识。新增仅放安全的任务/许可标识和版本；依赖、客户端和凭据继续由装配层闭包持有 |
| [`channel.py:ChannelService.accept/process/ResultDelivery`](../../../src/xiaowei/channel.py) | 入站真实性、指定群、成员和原消息回复仍复用。它的消息/会话映射与回复位置属于真实入站事件；不能伪造飞书消息调用 accept 来运行定时任务 |
| [`evidence.py:record/validate_answer/_checked`](../../../src/xiaowei/evidence.py) | 可复用来源、归属、当前读取权限、期限和事实生成；没有 Webhook 接收方范围。需增加目的地投影复核，包含本轮模型见过的全部相关 Evidence，而不仅是模型最后选择引用的部分 |
| [`feishu.py:send_delivery/send_outcome/LarkTransport.send`](../../../src/xiaowei/feishu.py)、[`feishu_delivery.py`](../../../src/xiaowei/feishu_delivery.py) | 可借鉴先验证、保存、单次发送、失败/未知分类。现有应用机器人依赖 chat_id/reply、message_id、uuid 与文件分项，不能直接用于自定义 Webhook，也不能把 requests resend 接到新通知 |
| [`storage.py:APP_SCHEMA_VERSION/InstanceLock/upgrade_storage`](../../../src/xiaowei/storage.py)、[`session.py:PolicySession/cleanup_expired`](../../../src/xiaowei/session.py) | 当前 schema v12，正式 SQLAlchemySession + PostgreSQL，单进程锁和显式迁移。需任务/运行应用表及清理引用；不能直接改 SDK 私表或复用依赖原消息的请求行来伪造运行。清理器要识别活动定时 Session 和结果保留关系 |
| [`runtime.py:_PrometheusQueryResult/_PrometheusWindowResult`](../../../src/xiaowei/runtime.py)、[`mcp.py:_prepare_prometheus`](../../../src/xiaowei/mcp.py) | 已有 PromQL、selector、时间/点数与 HTTP 容量边界，但查询主要结果是 `result: str`。不能据此可靠恢复标签、series、类型和样本 |
| [`test_prometheus_p2_protocol.py:test_official_range_text_is_ambiguous_even_when_it_looks_parseable`](../../../tests/sdk_core/test_prometheus_p2_protocol.py) | 已有可复现证据：不同结构化序列可落成相同官方 MCP 文本。D 不得猜解析此文本来计算“所有主机正常”；旧曲线 D4 的暂缓结论仍有效 |
| [`pyproject.toml`](../../../pyproject.toml)、[`uv.lock`](../../../uv.lock) | 锁定 Agents SDK 0.22.3；生产已有 httpx2（本地安装 2.13.1），httpx 仅 dev。优先复用 `httpx2.AsyncClient`，不新增重复 HTTP 客户端依赖 |

### SDK 公开能力及离线核验边界

锁定版公开 `Agent`、`Runner.run`、`FunctionTool.needs_approval`、`RunState` approve/reject/resume 和 `SQLAlchemySession` 已有本仓库真 Runner 测试：[`test_sdk_contract.py`](../../../tests/sdk_core/test_sdk_contract.py)、[`test_sdk_approval.py`](../../../tests/sdk_core/test_sdk_approval.py)。审批处理的是是否继续特定工具调用，不提供业务权限、长期任务授权、持久时钟或 Webhook 幂等保证。Session 不负责到期触发；不能为调度另写模型循环。官方参考：[运行及 Session](https://developers.openai.com/api/docs/guides/agents/running-agents#sessions)、[工具审批](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals)。

本轮可运行现有无网络 SDK 契约及文档检查；完整审批/存储/恢复闭环需实施阶段使用隔离 PostgreSQL。脚本模型证明调用链，不证明自然语言理解、真实模型质量、公司指标或飞书接收。

## 3. Agent 完成什么，以及可以自行决定什么

固定样例贯穿计划、测试、审查和最终验收；下表前四项沿用原用户验收标准，后三项来自后续已确认需求。

| 编号与用户任务 | 成功与失败时用户看到的行为 |
| --- | --- |
| U1：给 ops_alert_bot 发“今晚 22:00 检查 StarRocks” | 选对别名与原文，授权通过只尝试一次；成功：“已向运维告警机器人提交消息，Webhook 接口返回成功。”不能发错、重复或用 HTTP 200 代替业务成功 |
| U2：发往未经登记的 URL/机器人 | 目标不存在、未授权或输入 URL，网络前拒绝：“该机器人尚未配置或授权，未发送消息。”不探测 URL，不向模型列出它无权用的机器人 |
| U3：把刚才 fat 集群慢查询分析/巡检结论发到 ops_alert_bot | 用户已允许这类转发。复核原 Evidence 当前可读、接收方获准且内容范围合规，再发送实际摘要；缺证据或接收授权则说明未发送及原因。不能从数据库查询权推导所有群可收完整原值 |
| U4：测试消息遇到失败/超时 | HTTP/业务明确拒绝说失败；请求可能已到达而回执丢失说“发送结果未知，未自动重发”。异常不吞、不虚构成功、不无限重试 |
| U5：明天上午 10 点把汇报提醒发到大数据群 | 先展示绝对日期、时区、目标、最终文本、迟到策略并确认；确认后建立一次任务，到点用固定文本发送，结束后保留真实状态。重启/原 Session 到期不改变任务内容，不发送过去的错过提醒 |
| U6：从今天起每 3 小时巡检所有 Node Exporter 主机并发到大数据运维告警群 | 先明确源、节点范围/口径、锚点、期限/预算与目的地，批准后每次独立调查并发送覆盖、异常和未检查项；无数据/部分失败不能叫全部健康。每次发送健康结果也发，后续“仅异常通知”另作产品变更 |
| U7：把已有获准只读能力设置为一次或周期任务 | 可自动执行集合内才能创建；需自包含任务说明。未来能力逐项加入集合，不改调度逻辑。要求自动改配置、重启等不能成为无人值守任务；可留下建议，用户另发具体请求按原 Action 审批 |

**Agent 自主选择：** 在任务批准的工具/目标/数据范围和预算内，选择已授权源及工具、编写/修改只读 SQL 或 PromQL、补充取证、判断证据是否足以结束。示意调用可能是发现指标→取窗口→查局部异常→总结，也可以直接使用已知指标；不是固定顺序，也不要求先 Grafana 再 Prometheus。D 的基础覆盖检查是本用例的完成口径，调度器不硬编码它。

**需要的信息：** 当前时间/时区、身份、群、允许工具/源/机器人及限制来自可信运行上下文；指标类型、实例标签、当前目标、窗口和缺失情况来自受治理工具；现有获准 Evidence 可支持 U3。机器人别名歧义、必要业务口径、日期/时区歧义、无法确定的数据范围、任务期限/费用上限缺失时先问用户。服务端固定默认值可以展示后确认，不能猜目标群或索要凭据正文。“刚才/照上次”须在创建时解析成自包含指令和已确认参数，所需业务事实在未来执行时重新取证；不能把历史事实粘入 brief 或把原聊天 Session ID 当永久任务定义。首版历史快照定时转发的范围建议见下文。

**错误与继续：** 既有契约允许的参数/表达式错误、selector、已分类容量拒绝，可根据工具有限提示在原任务范围内修正；单次预算不因修正重置。缺查询列可发现结构，时间窗口只有批准可调整时才缩小，并说明覆盖变少。鉴权失效、源不可用按现有 source_stopped 停止本源，可调查其他已批准源并报告不完整；权限/Evidence 失败、任务版本失效、存储/一致性失败停止相关发送，不能删引用洗白。无人值守需要用户澄清、SDK 暂停或模型失败时结束本轮为未完成，不等待无人回应、不自动改任务；任务状态记需要处理，后续是否暂停见 §5。已开始通知不因模型自我修正、取消或重启而再次发送。

## 4. 工具与跨层接口契约

以下为必须确定的跨边界 schema，不预先指定普通内部函数。建议工具名可在开发时据 SDK schema 核验修订，但语义不能悄悄改变。

| 能力 | 模型输入、适用问题 | 结果限制和可修正拒绝 |
| --- | --- | --- |
| `send_webhook_message` | `recipient_alias` + 一种内容来源：明确 `text`，或当前获准 Evidence 标识及摘要要求；用于 U1/U3。禁止 URL、headers、签名、重试参数。内容来源须在可信许可中绑定，不能只信模型写“这是用户原文” | 返回安全的 receipt_id、别名、accepted/failed/unknown/not_sent 及有限原因。目标歧义先澄清；超长可在尚未占用发送机会前缩短摘要再批准/调用；失败/未知后不能在同一发送意图中换 call_id 再发 |
| 任务提出及执行请求 | 模式仅 `fixed_message/agent_task`；规范化时间 at 或 interval、目的地、自包含指令/独立提醒文字、读范围与预算。不得把历史业务事实或模型诊断改成无来源文字。提出为 propose；经已有 SDK/Action 确认后，本地 write 只激活或修改任务记录 | 代码生成展示材料与版本，返回 job_id/状态/next_run_at；过去时刻、歧义或不允许能力可在创建前修正。历史快照转发拒绝创建，可让用户选择到点重新调查；不能默默改变所求时间语义。确认时信息变化须重新展示，不能自动加大范围。任务激活不调用 Webhook |
| 任务查询、暂停、取消 | 仅获准管理者可查/操作 job_id；状态查询可 read，状态变更统一经本地任务管理 Action。暂停/取消确认的是停用动作，不是再次批准旧计划；恢复/扩大范围重新展示并确认全部计划 | 只暴露已获准的概要/回执，不返回凭据或其他群数据。运行中停用撤销尚未准入的读取/发送；已准入 I/O 可能完成，状态如实记，不承诺回滚消息 |
| D 结构化 Prometheus 读取 | 已授权 source_alias、受限只读 query/query_range/目标发现参数；用于基础指标与必要补证。源端 URL/认证由服务端配置，禁止任意 HTTP 路径/URL、label 值驱动访问主机 | 返回明确 resultType、必要标签、数值/时间、warnings、覆盖/限制及 Evidence。非法表达式/selector/容量在治理许可内可修正；未知格式、关键标签丢失、非有限值、缺样本不能解析成零或健康 |

### 独立通知授权与最小 effect 变更

现有 read 权限不含外发，write 又要求监控式单次 Action；把通知塞进任一语义都会错。建议增加唯一窄 effect `notify`，只允许本地 Webhook Adapter：

- 动态隐藏无权限通知；调用复核指定操作群、当前成员/主体、通知权限、目的地、内容来源、读范围、预算和许可版本。即时明确发送请求本身可作为该次通知的意图，不再额外弹一次确认；服务端绑定入站请求、目标及内容，重复工具调用只能得到同一回执。原文发送必须与真实入站文字中的发送内容一致；无法确定性绑定原文则先展示内容请用户确认，不让模型自己声明授权。模型推断的主动外发不成立许可。U3 同样须有用户明确发送要求及数据范围校验；模型参与改写的摘要校验全部相关上下文的接收范围，不仅校验最终引用。
- 定时通知的许可由已确认 job、当前授权和 occurrence 生成，绑定唯一发送意图；正常后台业务工具只允许登记为自动执行的 read，调度边界的通知仅发送校验后的最终结果。Agent 不能自行加机器人、多发中间结果或创建新任务。
- `notify` 不接受生产 Action 的批准来扩大范围，也不能赋予任何监控 write。它是新增产品契约，需独立审查；影响仅工具筛选、通知准入与回执。`RunContext` 至多增加可信许可 ID/任务版本，不能携带许可服务对象。

任务激活/变更推荐复用已有 `ActionBinding` 的 propose→SDK 暂停→指定群 `/批准`→本地 write→回读任务记录。这里批准的是明确的应用内任务配置，不是假借监控写或把 notify 注册为 write；沿用同一审批人来源、材料送达和单次执行。需为本地任务绑定登记权限、状态投影与真实适配，现有 ActionService 不能凭一句“复用”自动支持。B 首先做隔离最小贯通验证；若必须大改监控审批，先停止并重新评估窄扩展，禁止新建第二审批引擎或放宽原守卫。

### 持久化数据与调用链

最少增加应用自有 `scheduled_job`、`scheduled_run` 和共用 `notification_attempt` 记录（表名前缀按现有规范）。即时通知也需 attempt；不把完整消息塞进原入站请求表。应用迁移显式升版，SDK 表不变。

- Job：不透明 ID、版本、创建者、原操作群、状态、批准记录/时间、自包含指令或独立提醒文字、必要的非秘密任务参数及其可信来源类型、目的地绑定、源/工具及现有数据权限范围上界、Model Profile/契约摘要、时区/绝对时刻或间隔锚点、next_run、独立有效期、次数/预算。参数如源别名在执行时按当前可信配置重新解析，不把采集所得数值、结果表或诊断结论存成任务参数。批准绑定当前实际接收方的服务端摘要；不得保存原始 URL 或用凭据拼接普通 hash 后暴露模型。
- Run：唯一 `(job_id, job_version, scheduled_at)`、新 session_id/turn_id、开始/结束、独立执行状态、实际预算、最小合规回答/Evidence 引用及保留期限。固定提醒没有模型 Session；完整结果与投递不是同一个成功标志。
- Attempt：唯一 intent_id（即时基于原请求与已绑定内容，定时基于 occurrence）、安全目的地标识/版本、获准最终 payload 摘要、准入及结束时间、发送状态/有限错误码。为恢复核对保存的文本也经过数据投影和保留限制，不存密钥；日志只记安全 ID/状态。

链路：真实群消息→既有身份和治理→Agent 提出/确认任务→Action 原子激活 job；到期循环原子创建并占用 run→复核任务/当前成员及权限→自包含新 Session→`Application.run_turn`→保存已校验结果→当前接收范围与全部相关 Evidence 复核→保存/原子占用 attempt→`GovernedTools.invoke(notify)`→HTTP Adapter→落定回执。固定提醒从运行准入直接走通知，不伪造 Agent 回答。占用数据库事务不得跨外部 I/O；服务退出先停止新准入，再有界收尾，不能关闭客户端后留下继续发消息的协程。

**历史数据不进入长期定义：** 原 Session 期限不限制 job 本身，但原数据的期限仍限制数据。建议首版仅支持独立用户提醒文字和到点重新取证的任务，不支持“明天转发刚才结果”的历史快照；U3 的即时转发保留。这项范围建议避免引入长期 Evidence 依赖/复制保留机制。创建材料用代码组装，指令、可信参数和独立用户文字与历史事实来源分别核对；从当前已知业务上下文摘出的数值、结果或诊断，不能由模型换字段名声明为普通 text/参数。若无法证明是独立文字或无事实的指令，就在入库前拒绝/澄清，不通过再次确认洗掉来源。固定消息路径和 Agent brief 路径均在入库及读取入模/发送前执行此约束；遇到无法复核来源的定义暂停任务。未来若要支持历史快照，须另定最小依赖、当前读/接收权限、到期及保留，不能只延长任务 TTL。

## 5. 时钟、权限变化与失败状态

**建议默认值，待本设计审阅确定：** Asia/Shanghai；at 转成 UTC aware 时间保存，确认显示业务时区和绝对日期；interval 从批准的锚点计算，不随上轮结束漂移。U6 确认后先执行一次，再按锚点每 3 小时。到期检查约 5 秒、最多允许迟到 60 秒，超过记 missed，不追赶补跑；单任务不重叠，下一时点若前轮仍在运行记 skipped_overlap。只设一个后台执行槽，同时计入现有全局并发。正常负载下验收到期后 60 秒内开始；模型和网络耗时不承诺整条消息精确在某秒送达。

每个 job 必须有独立有效期和运行/通知次数上限；长期用户意图可以通过续期明确批准继续，不能借原 Session 保留期偷偷到期，也不能默认无限费用。建议首批有效期 30 天、3 小时任务最多 240 次，确认时展示；具体接受与否见 §8。单次模型预算复用现有策略，不等于硬美元限额；模型调用可能多步，8 次任务/日不等于 8 次 API 调用。并发、任务数、外部请求/样本上限是服务端上界，不由模型加大。

权限取交集：任务批准上界 ∩ 当前用户/指定群授权 ∩ 当前自动执行集合 ∩ 当前目的地数据许可 ∩ 当前预算。创建和每次运行重新核验成员，发送前再次核验任务状态、成员/授权及接收范围；工具调用复用当前 authorize，不在相同状态重复全量查目录。撤权、成员不可确认、目的地/源/模型/Profile/关键契约绑定改变使 job 进入 needs_attention，不自动扩权或换源；权限恢复后也须有权用户恢复确认。单次源超时或短暂模型失败只记该轮未完成，后续独立时点可继续；需要人工澄清/审批、范围或存储一致性失效则暂停任务。数据量增长超过批准预算报告不完整，不能自动放大。

| 状态边界 | 持久状态与恢复规则 |
| --- | --- |
| 运行未开始/迟到/重叠 | pending → running 只占用一次；错过/重叠为终态 skipped，不执行工具。重启跨过的计划时点记录或汇总为 missed，计算下个将来时刻，不生成无界补跑记录 |
| Agent 已开始但进程中断 | 运行记 interrupted，Session 不续跑；不重放已读工具或通知。未来 occurrence 只有任务仍有效、上一轮已终结才可开始 |
| 最终结果保存失败 | 不发送；读执行可能已发生，不能重跑补保存。按既有 Session 一致性封存并记录安全故障状态 |
| 通知准入前拒绝 | not_sent；可明确说未发送。只要还未占用 intent，可按返回的可修正原因调整；不能通过改 ID 绕过已经准入的发送 |
| 明确业务拒绝/有证明尚未发送的连接失败 | failed；HTTP 错误只有可判定接口拒绝才算确定失败。DNS/TCP/TLS 建连未写请求可记失败，未经证明的异常保守记 unknown；本版失败也不自动重试 |
| 请求已开始、写/读超时、响应损坏、进程在 POST 后崩溃、接受后回执落库失败 | unknown；包括“已占用发送机会但无法证明是否执行”的崩溃窗口。优先不重复发送，允许少发；无自动再次 POST，无自动重跑模型，无 requests resend 路径。用户人工核对后明确另发新消息是新的发送意图 |
| 接口明确接受并回执落库 | accepted；仅说明 Webhook 接口成功接受。不能声称群成员收到/阅读 |

未知状态不自动取消未来周期，但保存为独立可查询的该轮状态，下一轮含自己的计划时间避免混淆。如果查询结果不可用，可按确认的通知范围由代码生成一次“本次巡检未完成/部分未检查”通知；它使用该轮同一发送机会。通知本身失败不向同一 Webhook 递归报警、不额外发第二条。

这里只承诺本地自动尝试至多一次，不承诺跨数据库和飞书 exactly-once。停机、凭据撤回、成员接口故障会牺牲及时性；该代价必须在任务确认和运行状态中可见。

## 6. Webhook 和结构化 Node 巡检

### Webhook Adapter

复用 httpx2 AsyncClient 生命周期，TLS 校验、有界 connect/read/write/pool 和整次期限、响应字节上限；不启用重试、重定向或隐式环境代理。仅管理员配置的官方飞书 HTTPS hook 主机及路径可用，不从用户/数据内容构造 URL，不跟随响应或 scrape URL。首版不允许内部代理和自定义域名；若公司必须代理，需单列可信代理配置与 SSRF 验证，不能静默使用 `HTTP_PROXY`。禁止任意 URL 入口与重定向是当前边界，主机/PATH 校验是服务端配置错误的早期检查；不能声称已解决任意被劫持 DNS 的所有网络风险。

消息仅 `msg_type=text`，不得按模型输入转成卡片或群 @all/成员提及；最终 JSON 序列化后的完整请求不得超过官方 20 KB，含签名字段。可采用保守的 20,000 字节上界及更小业务摘要上界。服务端生成时间戳/签名，不进入模型；官方关键词、IP 白名单和签名配置分别验证，不能假设签名替代其他实际启用的规则。按机器人维度复用预算/限速，以官方 100 次/分钟、5 次/秒为上界，不引入独立消息队列。

HTTP 成功后严格检查受限 JSON 响应的业务 `code`，只有合法整数 0 才接受（拒绝 bool 等错误类型）；非零业务错误为失败，包括 HTTP 200 的拒绝。HTTP 4xx/429 等确定拒绝与 5xx/损坏响应的未知按 §5 分类；不把原始响应 msg、exception、request repr 写进日志/Evidence。若公司实际响应只有旧 `StatusCode`，先留样本脱敏核对再决定兼容，不盲目以任一字段为零放行。参考[飞书自定义机器人](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)和 [HTTPX2](https://pypi.org/project/httpx2/)。自定义机器人没有在本次官方资料中得到可依赖的幂等键/回执查询契约，因此不套应用机器人 uuid。

### Node：结构化取数是前置条件

推荐一个本地、受治理的 Prometheus 只读 HTTP Adapter，读取标准 JSON `/api/v1/query`、`query_range` 与受限目标发现；绑定到现有同一个逻辑源，而不是新增一个公司数据源。需要管理员另配实际 Prometheus API 安全引用并核对与 MCP 指向一致；MCP Server 地址/凭据不能当作 Prometheus API 地址/凭据复用。若网络只允许现有 MCP，先让该外部 Server 提供经版本验证的等价结构化能力再做 D，不内嵌 Server、不猜解析现有文本。建议是新增接入路径，必须单列审阅；A–C 不依赖此路径。

Agent 可写受约束 PromQL 来取基础事实与补证；计算由结构化样本和可信代码/Prometheus 运算形成 Evidence，不由模型数文本猜数值。目标发现过滤 scrape URL、认证等字段，只保留必要 labels、health 与 last scrape 状态；它用于定义当前巡检范围，不能访问返回的各主机 URL。硬权限仍按 [ARCHITECTURE 监控源权限](../../../ARCHITECTURE.md#monitoring-mcp) 的源/工具/当前授权共享，不新增逐主机、指标或 PromQL 模板 ACL。Node 目标选择器只定义本次巡检分母和业务覆盖，是任务相关性要求；Agent 在同一获准源补证或错误地偏题，不应被描述为越过一个并不存在的指标白名单。偏题和不必要读取由固定任务能力评测约束；跨源、无权工具及资源超限仍在 I/O 前拒绝。若要按指标/标签硬隔离，须另提产品权限变更，本期不建 PromQL 权限解析器。

最低覆盖口径：

- 从批准 Node 目标范围及当前目标清单建立分母，包含 down exporter，不从“有 CPU 数据的 series”反推全部主机；保留 job/instance 等标识，不假设 `job=node` 或把相同主机名的多个 exporter 随意合并。清单只是当前发现的目标，不证明未知/已删除主机存在；这项限制要展示。
- 每次看最近 3 小时及当前状态；CPU 使用、available memory、磁盘/文件系统容量及 inode、磁盘 IO 与网络收发按真实指标类型和单位查询。counter 先 rate 再聚合，处理重置、缺测、NaN/Inf、重复序列和查询 warnings；临时文件系统/虚拟接口过滤、CPU 口径、多网卡及磁盘聚合要在 D 的取证中确认。
- 无 scrape 数据只叫“采集不可用/未检查”，不能直接说服务器宕机。缺内存/IO/网卡速率等指标分别标缺项；有效覆盖不足、返回截断或达到样本预算时不能报“所有主机健康”。API 内部多次读也计入请求数/期限/字节与样本预算，不能只统计一次外层 tool 调用。
- 建议先用参考阈值进行巡检，不自动创建告警规则：CPU ≥90%、可用内存 <10%、文件系统或 inode 可用 <10%；分别展示持续/窗口和实际值。网络利用率只有可信正链路速率时才计算，≥80% 可作关注项，否则仅报流量和趋势；磁盘 busy/iowait 不能用统一阈值断言存储故障。阈值/持续时间为待确认巡检口径，不能拿 Grafana 默认颜色当业务标准。
- “持续 15 分钟”不得偷换成 15 分钟平均；D 要按真实 scrape 间隔定义充分采样与连续性，缺样本不能满足持续条件。未取得这些信息前，只允许报告观察窗口内均值/峰值和缺测，不能输出已确认的持续告警。实际规模、采样间隔、series 数和阈值由获准预检确认后绑定口径版本。

短消息包含计划/实际时间、源和范围、检查/异常/缺项计数、必要主机指标、获准分析与省略说明。正常也发送本轮摘要。逐项事实从 Evidence 生成；分析单列为判断，不宣称已经修复。若异常太多放不下，发有界摘要及明确省略数量，不多次拆分 POST 或附全量查询结果。

官方依据：[Prometheus API](https://prometheus.io/docs/prometheus/latest/querying/api/)、[rate 等函数](https://prometheus.io/docs/prometheus/latest/querying/functions/)、[Node Exporter](https://prometheus.io/docs/guides/node-exporter/)、[Linux IO 统计含义](https://www.kernel.org/doc/html/latest/admin-guide/iostats.html)、[Grafana 面板阈值](https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/configure-thresholds/)。这些文档不证明公司版本、指标或口径已经验证。

## 7. 选择、成本与重新评估条件

| 选择 | 推荐及主要代价 | 备选与重新评估条件 |
| --- | --- | --- |
| 接收方 | 管理员静态登记 alias/说明、安全引用、可接收范围；配置少，授权可审查 | 对话给任意 URL 会泄露 bearer token、扩大 SSRF/接收边界，不能接受。未来自助管理须独立产品设计 |
| 时间触发 | asyncio 小循环 + 已有异步 PG，at/interval 足够；仍要写业务状态/恢复语义 | APScheduler 有 misfire/max_instances 等现成能力，但其 3.x SQLAlchemy JobStore 使用同步 engine/序列化任务，仍需自己的授权和回执表；当前增加升级/双状态成本。复杂日历、夏令时、多进程/HA 才重评；不预建分布式锁/lease |
| 即时确认 | 明确发送指令+当前独立权限即可，少一次交互；U3 仍校验内容来源/接收方 | 每条再确认更保守但重复。若需模型重新编写敏感业务摘要且许可无法确定来源，先展示摘要确认或拒绝，不靠提示词授权 |
| 定时确认 | 借现有 Action 激活/变更；新 job 范围与有效期成为持续授权依据 | 延长原 Action/保存 RunState 当定时任务会错误继承原 Session/预算/审批，不采用。若本地绑定要大改监控 Action，B 停止重新定界 |
| 运行形态 | 一个后台槽+现有全局并发；部署简单，忙/停机允许跳过 | 新 Worker/队列/服务对当前规模不值得。任务延迟或人工运维成本超过可接受范围时再据真实负载评估 |
| 摘要外发 | U3 及定时业务结果可发至获准机器人；复用 Evidence，多一层接收投影复核 | 只允许文字会不满足已确认需求；转发整段历史/所有原始结果超出授权，不采用 |

## 8. 尚需确定及正确性门槛

以下不阻止文档完成，但阻止依赖它们的功能开放；不能把建议写成实测事实。

1. **产品默认值：** 建议时区 Asia/Shanghai、U6 立即首跑、忙/离线迟到 60 秒后跳过、30 天有效期/240 次、每轮正常也发送。用户需审阅这一组可观察取舍；若要求补跑、永久任务或精确准点，需要重新评估成本与重复发送风险。
2. **权限由谁配/谁批：** 建议延用指定群的现有审批人来源，但通知与任务管理有独立配置项；目的地允许哪些源/工具由管理员登记，创建者当前须有同等权限。首批负责人/目的地别名和策略尚未提供，只确认安全引用方式，不索要 URL/密钥正文。
3. **SDK 与 Action 适配：** B 需要真 Runner + 隔离 PG 证明本地任务绑定、SDK 暂停/恢复、材料送达、原批准过期与独立 job 授权可共同工作；本轮源码阅读不等于贯通通过。notify effect、即时原文/业务摘要许可和后台准入的零 I/O 拒绝必须分别证明。
4. **Node 取数接入：** 需要获准确认实际 Prometheus API 可达性/认证、MCP 源一致性、Node job/标签、scrape 间隔、实例/series规模、仪表盘所用查询和链路速率有效性。不用当前唯一数据源这句话推断已经具备 HTTP 网络路径；结构化能力不满足则 D 阻塞。
5. **Node 判断策略：** 上述阈值、文件系统/接口过滤和充分采样条件未获公司实测；建议先按参考口径标“巡检发现”，验收前固化准确公式、阈值、缺测规则和上限，不能宣称通用健康保障或替代常驻告警。
6. **真实验收授权：** 固定任务样例必须在具体获准模型/Profile、公司只读范围和独立测试机器人验证，费用/次数/数据范围另定。离线、真实模型、真实指标、Webhook 接口接受、群内人工观察、部署、用户接受分开记录。未获准前只用合成数据/替身和隔离 PG。
7. **历史快照的最小范围建议：** 首版保持 U3 即时结果转发，定时消息只支持独立用户文字或到点重新取证，不把历史业务快照持久化为普通文本。若业务要求定时发送过去某次的原结论，应单独确认依赖与保留方案；不能假定本次任务 TTL 已授权延长数据 TTL。

## 9. 文档核验记录

本轮核验命令、结果、精确文档审查版本和剩余缺口集中写入 [AGENT_HANDOFF](../../../AGENT_HANDOFF.md)，不在这里复制历史测试统计。开发阶段在 A–D 各片冻结 SHA 后按计划审查，已有 CI 或本轮文档检查不能替代功能验收。
