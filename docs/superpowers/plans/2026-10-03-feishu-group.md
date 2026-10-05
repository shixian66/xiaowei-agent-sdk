# 飞书单群：共享会话、有界排队与当前发起人授权实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. 按依赖串行交付，每片提供提交 SHA 和独立审查；计划通过不等于实施、合并或部署授权。

**Goal:** 指定群成员 @小维 后能用自然语言查询、诊断并共同追问；两人同时提问按群内接受顺序处理，权限、事实来源和回复归属始终清楚。

**Architecture:** 扩展现有 FeishuGateway → ChannelService → Application/PolicySession → GovernedTools → Evidence/ResultDelivery。SDK 驱动 Agent；当前发起人和群会话归属分离，现有进程内有界队列支持一个群的 FIFO，不增加 Worker、Redis、第二套历史或多群管理。

**Tech Stack:** 沿用 P2.5 交付锁文件；本次规划核对的版本为 Python 3.11.16、openai-agents 0.22.3、lark-channel-sdk 1.4.0、SQLAlchemy 2.0.52、PostgreSQL 16。

**Spec:** 产品唯一权威为 [ARCHITECTURE §7 G1–G4](../../../ARCHITECTURE.md#group-scope)，自然语言用途引用 [§5](../../../ARCHITECTURE.md#turn-purpose)，当前数据权限引用 §9。数据库能力、受限只读执行、单 Agent 自然语言工具选择和数据权限验证由 [P2.5 计划](2026-10-03-p25-open-read-multi-cluster.md) 交付，此处只定义群边界。

**Baseline / 状态：** 文档候选 v2（2026-10-03，按用户对 aed344b 的决定修订）；P2.5 Task 8 经审查的阶段交付 SHA 为 PR #41 的合入提交 `a938a0f1b481f1cb23507dc170380c73028b8e54`，F0 起点为其后只改文档的 `origin/main` `e153cd9129fa1a39913cc6641dae68cd90637f19`（PR #42）。F0 已用锁定 SDK 与本机合成协议端点完成（证据见 F0 小节），独立复审针对 `f6875f9ccc44dabaa84607eb33644c61a0162d3e` 通过、无阻断。F1 经两轮审查修复后复审针对 `3140b76501418266c5c7afd43ccbb54b54954e1f` 通过、无阻断，F0/F1 分别随 PR #43、#44 合入 main（`ac2d32a`、`0b1bdee`）。F2 经首轮审查修复后复审 `200acd3` 通过，随 PR #45 合入 `6168c97`；F3 经三轮独立审查修复后复审 `8e1ace2` 通过、无阻断，随 PR #46 合入 `1178c74`；F4 离线部分经两轮独立审查（复审 `4de5d06` 通过、无阻断）随 PR #47 合入 `f32291b`；当前无真实群验证证据，真实验收在 P3。

## Global Constraints

- 范围为一个指定群，保留单聊与 Web；不做多群配置管理、跨渠道同步、逐人 ACL、写操作或卡片平台。用户确认全员同权共享结果，不必为每位成员维护单聊 users 映射。
- 遵循 AGENTS 的 SDK 原生、薄治理与按风险验证；普通函数名由实现决定。只调整直接承载身份、Session、队列和交付的模块，不重建统一调度框架。
- 群内收到的原始正文、提及、引用与昵称不可信；身份和回复位置只从已验证平台事件和可信配置建立。模型不会取得群配置、其他群历史或可任意指定收件人的发送工具。
- 当前授权只包含文档和有限离线验证。后续实施使用独立工作树、合成事件与隔离 PostgreSQL；真实飞书、模型、用户数据库留在 P3 获准环境验收。不动 `xiaowei-release-*`。
- 每片先做可失败的行为用例，再做最小实现；异常、取消和结果不明保持显式状态。中间提交不迁入运行环境。

## Review Focus

1. 共享历史的所有者与当前操作者是否真正分离，还是只把 sender_id 替换成群 ID（F1）。
2. 事件接受、排队、开始运行、最终发送及重发的身份与权限是否一致；离群后是否还能利用旧回执（F1–F3）。
3. 同群消息是否串行且有上限，是否占满工作槽等待同一把锁，使其他会话无法运行（F3）。
4. 去重、原消息回复、队列满/超时、落库与发送故障是否会引起二次查询或错群发送（F2–F4）。
5. 离线脚本证明的是门控与调用链，真实模型的多人指代和飞书平台契约是否仍明确未覆盖（F0、F4）。

## 1. 最小方案与调用链

### 1.1 复用与源码缺口

| 现有位置 | 沿用 | 必要变化 |
| --- | --- | --- |
| `config.py` / `runtime.py` | 可信租户、凭据引用、装配、进程锁与生命周期 | 可选单群配置、可信 bot 身份、群共享策略与成员校验依赖 |
| `feishu.py` 的解析/Gateway/Transport | 原始事件检查、线程桥、有界接收、纯文本渲染 | 可信 mention、群事件、原消息回复、同群排队与安全状态提示 |
| `channel.py` / `channel_store.py` | 接受/运行/完成/投递状态、去重、条件更新、恢复 | actor 与会话 owner 分离，群请求/目的地绑定、等待期限 |
| `models.py` / `session.py` / `evidence.py` | 受限 Context、SQLAlchemySession、回放/最终验证 | 群所有权与当前 actor 复核；保留每条事实的采集者 |
| `app.py` / `governance.py` | P2.5 单 Agent 工具选择、SDK Loop、业务前治理 | 消费本轮可信群范围，不另建群专用 Agent 或工具集合 |

当前 `_parse` 只接受 p2p 且 sender 必须映射配置 users；ChannelStore 的会话和请求键含 subject_id；Evidence/Session 也以个人身份绑定。只开放 chat_type 会造成每人独立历史或越权，须检查全链。当前消费者池从全局队列取任务，Application 遇同 Session 繁忙会拒绝；它不等于本计划的同群等待。当前发送只带 chat_id，`runtime.resend` 由操作者重填 chat_id；群结果须补齐原消息目的地绑定。

### 1.2 调用链

```text
SDK 长连接 raw 事件（沿用先 ack 的已接受限制）
  → app / tenant / chat / user / text / 时间 / 本 bot mention 校验
  → 可信入站身份与指定群共享策略检查（不查成员 API）
  → 持久接受：去重、actor、群 owner、原消息、当前 Session generation
  → 群 FIFO（有限长度/等待期限；必要时固定状态回执）
  → 出队：重验当前成员/授权/会话 → 获取一轮运行槽
  → P2.5 的历史复核 / 唯一业务 Agent 自行选择工具 / 受治理工具
  → Evidence 与 Session 按群归属、当前 actor、当前数据权限验证
  → 保存最终结果 → 获取现有投递权 → 当前权限再次复核
  → SDK 单次回复原群原消息 → 记录 sent / failed / unknown
```

等待澄清的本轮正常结束并释放槽位；下一条 @ 消息是新请求。排队后运行时才载入最新的已提交群历史，不能在入队时拍下旧 Session 后并发回放。

### 1.3 已核对的 SDK 能力与限制

仅阅读锁定版源码，没有真实飞书调用：

- `FeishuChannel.get_chat_members(chat_id, page_size, max_pages, id_type="open_id", force=True)` 是公开能力，可绕过成员缓存；返回用户列表、不包含 bot，API 失败抛出异常。内部有分页完成标记，但公开返回值不包含它，达到页数上限可能得到部分列表。因此找到当前 sender 可作本次成员证据；未找到只能按“无法确认成员”拒绝，不能声称已证明其不在群内。
- 公开 `send(..., opts)` 支持 `reply_to`、`reply_in_thread` 与 `reply_target_gone="fail"`。默认原消息消失会改发新消息；本计划显式选 fail，复用公开开关，不改私有 sender 或 monkey patch。
- 当前产品通过 raw 事件自行治理，SDK 的 dm_policy/group_policy 都禁用；F0 要验证群 raw 事件仍到达、字段来源及线程桥，不能靠放开 SDK 默认策略绕过小维入口。
- SDK 的传输 ack 先于小维落库是继承限制。没有可靠事件回放/至少一次处理保证；F0/F4 和 P3 分别证明可覆盖的边界，不把持久化后的去重宣称成端到端 exactly-once。
- F0 修正与补充（锁版实测，详见 F0 小节）：raw 回调在 SDK 去重、规范化与 policy **之前**触发，重复事件同样到达；SDK 规范化阶段会为每条非重复消息的发送者调用通讯录接口（早于 policy 拒绝），公开构造参数 `name_lookup` 可关闭；不带 `force` 的成员查询会返回此前缓存的完整旧名单；没有业务 `code` 的 HTTP 错误被 SDK 当作成功（无 `message_id`），本次已在 `send_outcome` 修正为结果不明；回复原消息时官方的 230011（已撤回）/230050（对操作者不可见）被 SDK 归为 `unknown`，本次按 `raw_code` 记为明确失败；SDK 归为 `target_revoked` 的 230020 在官方文档中是单群限频码，默认 `fresh` 会把限频改发成新消息，`fail` 下仍为明确失败。

## 2. 跨边界契约

### 2.1 单群配置、入站和成员权限

- 可选配置只允许一组 app/tenant/chat 与明确的共享查询策略，空配置仍是当前单聊。bot 的 open_id 来源须与 app 匹配、由可信配置或受测的 SDK 身份接口取得；不得用显示名称识别本机器人。
- 入口逐项检查消息类型、sender_type=user、app/tenant/chat、平台 mentions 中本 bot ID、时间与正文大小。只删除与平台 mention 对应的 token，再处理命令/自然语言。纯文字 `@小维`、@其他 bot、缺失/伪造 mention、其他租户/群、机器人和超龄事件均不进入 Agent、模型或数据库；不向非指定群回复。
- 保留 tenant_id（映射平台 tenant_key）、chat_id、sender_id（open_id）、message_id，并连同 app 与入口种类形成可信身份。群用户不需要出现在单聊 users 中，群授权不得使该用户自动获得单聊/Web 访问。
- AccessPolicy 接受可信群上下文。采用用户认可的最小方案：**成员目录只在出队开始执行和最终发送前各查一次**，公开 SDK force=True、固定页数与期限，未找到/失败均不放行；同一轮内部工具复核沿用本轮已确认的 actor 成员证据，仍独立检查目标/工具/数据库权限。接受排队仅校验可信平台事件与指定群配置，不赋予查询权。经 ResultDelivery 的每次读取与发送（含 `access_denied` 等无数据失败回执）前都查一次成员，持续无法确认时不发送、不取得投递权，请求保持待投递；不经 ResultDelivery、不含任何数据的排队状态提示（F3）不查目录。独立历史读取/显式重发各自在该次访问前查一次，不沿用上轮名单。
- 成员目录只给治理使用，不把全群名单交给模型/Session；不配置可替换成员真实性的 resolver hook。页数/期限在配置中有有限上界，F0/P3 确认指定群规模能覆盖。群规模超出上界时，部分名单中找到当前 sender 仍是本次成员证据；未找到只能视为无法确认（不声称其不在群），按现有规则不执行、不回复——此时连“能力不可用”的提示也不能安全发送（发送前同样无法确认成员），用户只会得不到回复，须由运维按群规模调整页数上限（P3 实测）。
- 两次成员检查之间有明确窗口：执行中离群不保证立即终止已获准的本轮只读调用，最终发送前必须重新确认；不能把每次工具复核说成每次都查过成员 API。已发消息不自动撤回，配置移除群则拒绝新接受及后续执行。

### 2.2 actor、owner、Session 与 Evidence

- actor 是当前 sender 的可信身份；owner 是应用/渠道/租户/群所决定的共享会话归属。两者是不同字段/含义，不用“假用户 ID”同时充当两者。私聊与 Web 沿用个人归属，不因群改动放宽。
- 群 Session 由 owner + generation 及已有 ModelBinding 约束生成；同群不同 sender 使用同一当前 Session。`/新建` 在空闲时轮换整个群，保留期与历史上限沿用现有契约；运行中或有排队请求时拒绝，避免接受时与执行时绑定不同代。
- 每条请求/Evidence 保留原 actor、turn 与目标来源；共享可读性按当前 owner、当前请求者资格和 P2.5 数据权限复核，不能通过抹去 actor 或取消 ownership 检查实现共享。A 收集的事实在同群可由 B 追问，其他群/私聊/Web 不可读，即使是同一个人。
- 历史需要区分当前发起人与各轮作者，利用既有请求记录与 SDK 公开输入格式给出可信、有限的作者/轮次标识；不得依据正文“我是管理员”推断身份，不建另一张聊天历史表。保留 SDK tool call/result 配对，角色/标识的投影也计入容量。模型能理解标识不等于取得额外权限。
- B 回答 A 的澄清并不自动继承 A 的查询许可；本轮从 B 的消息及可读群上下文重新判断。信息指代不清时澄清，不能任取最近一人的意图。数据库工具与单 Agent 自然语言行为契约直接引用 P2.5，不在群层另写一套。
- 增加应用表迁移以存储 owner 种类/范围、原 actor 和受信回复目的地。旧个人记录保留个人归属，不自动归入群；无法无歧义迁移的绑定要求新建。SDK 表仍由 SDK 维护；若 owner 指纹格式需要版本化，必须证明个人指纹保持原值或明确记录受影响的一次性失效，不能意外全量失效。

### 2.3 去重、回复目的地和交付

- 群 request key 绑定 app/tenant/message_id，接受后保存 chat/actor/正文与用途摘要、Session/turn/原消息；同 ID 同内容只返回原记录，同 ID 换群/换人/换正文拒绝冲突。不同 sender 不能利用各自的键空间把同一消息运行两次。
- 回复目标从已验证入站事件建立并持久化最小 app/tenant/chat/message_id（受应用表保留期保护）；模型、正文和重发命令不能替换目的地。发送前核对当前配置、群资格、原绑定和数据权限。
- 复用 SDK 公开 send/reply 参数，纯文本单条上限与 P2 渲染顺序保留，来源中包括集群。群回复指定原 message_id，选择 `reply_target_gone="fail"`、单次发送；原消息删除则明确失败，不自动改发私聊、其他群或无关联新消息。
- 排队状态回执与最终回答分开：最多在本次新接受入队后发一次无数据的“已接收，将按顺序处理”，不许诺精确位置/等待时长；重复事件不重复通知。通知失败/结果不明不重试、不取消已经持久接受的工作，也不占用最终结果的投递状态。**原有“只发一条”指每个最终回答，此处另允许一条排队状态提示。**
- 最终回答沿用持久化、Evidence 重验、投递 attempt 条件更新；同群当前请求者可追问共享结果，但发送状态变更仍绑定原请求，不借用另一个人的 receipt。发送失败/未知不能重跑 Agent/SQL；明确重发按既有受限维护入口操作，只能发往保存的群目的地，并按当前权限复核。
- runtime.resend 对群需装配当前成员/数据验证和读取保存目的地；个人路径保留原绑定约束。群权限无法确认或旧记录没有群目的地时拒绝，不回退到调用者传入的 chat_id。不新增群内任意重发/导出工具。

### 2.4 有界 FIFO 与生命周期

- 同群按**系统持久接受并入队的顺序**执行，不承诺按飞书 create_time 排序。接受/入队/新建会话在该群短临界区内完成，持久化等待也有期限；入队结果不明锁低 readiness，沿用恢复把未完成请求标为 interrupted，不能正常接活却把请求遗忘。
- 复用现有有界队列/固定消费者，增加单群 FIFO 和一个 active 标记；只把可运行的队头交给消费者。同群剩余任务留在有限队列，不创建无限 asyncio task，也不占住全部消费者等待同一会话锁。下一轮在上一轮结果保存与一次投递尝试落定后才开始，避免回复乱序；单次投递有期限。
- 明确群等待数量上限（不计正在运行的一条）和最大等待时间，均使用正数有限配置；与全局 queue_size、consumer_count、整轮并发和目标连接上限一起校验。等待时间从接受时起算、不能由重投刷新，不占 SDK/model 预算；出队后整轮期限按现有规则计算，同时不越过请求保留期限。
- 队列满立即保存受控失败/固定回执；到期项在有界定时检查或出队时结束：检查间隔只约束到期检测与状态结算（failed/busy），固定回执另行有界发送，平台实际收到的时间还受成员查询、网络与发送期限影响；不等前一条无限结束。满/到期/成员失效均不调用模型或数据库，不重新入队。队头失败或普通取消仍能释放 active，使下一条继续；存储/实例所有权故障则停止渠道，不能带着不明状态继续。
- 等待人类澄清不占队头：提交澄清回答后结束该轮，其他成员可继续；下一条回答仍需 @ 并按新请求接受。不开 SDK interruption 长时间挂起，也不实现会话续跑框架。
- Web/单聊保持原来的同会话繁忙提示；不同会话可以在全局上限内并行。群内跨两个集群仍属一个共享 Session，必须串行。
- drain 先关入口，在同一绝对期限等待 receive、群/普通队列和在途发送。恢复时 accepted/running 都标 interrupted，不重放模型/SQL；**仅排队未开始的群请求不关闭原 Session**。当前 channel_store.recover 对两类都调用 close_interrupted_sessions，F3 必须区分中断前状态；同 Session 存在 running、写入不明或其他原有不一致则照常关闭。成员任务和传输线程须关闭，不允许退出后继续发送。

## 3. 失败处理总表

| 场景 | 对用户的行为 | 需要证明的边界 |
| --- | --- | --- |
| 无 @ / 非指定群 / bot / 伪造身份 | 不回复 | 无 Agent、模型、业务 DB；不触发无关成员查询 |
| 当前成员或共享策略无法确认 | 开始前不能确认记 `access_denied`；固定拒绝回执发送前同样查成员，确认后才发送，持续不能确认则不回复 | 无旧事实/名单泄露；无 Agent、业务 DB |
| 同群已有一轮 | 有界排队，必要时固定提示 | 新任务未提前读取历史/调用模型；其他会话有槽可运行 |
| 队列满/等待到期 | 明确未运行，可稍后重新发问 | 记录失败；模型与 DB 调用为 0；重投不刷新期限 |
| 排队中成员离群或群配置撤销 | 未获准执行 | 出队重验失败，无历史回放/模型/DB |
| 当前 SQL 用途不明 | Agent 应澄清并结束此轮 | 查询工具仍按授权可见，语义由 P3 评估；澄清结束释放群槽位 |
| PostgreSQL/实例锁/入队交接失败 | 服务暂不可用 | readiness 关闭，不遗留假“处理中”并继续接活 |
| 原消息删除或发送失败/未知 | 保存真实投递状态 | 不换目的地、不重查、不自动重发 |
| 确定撤权或 Evidence 失效 | 不交付旧事实，必要时新建会话 | 与 P2.5 同一验证器，群共享不能绕过 |
| 集群暂时无法验证或权限检查超限 | 本次暂不可用，保留原群 Session | 不交付旧事实、不重跑业务 SQL；恢复后新请求重验 |
| 重启/停机超时 | 请求中断或投递未知；仅排队不关闭安全群 Session | 不重放模型/SQL，已开始或写入不明仍关闭 |

## 4. 按独立结果拆分的任务

依赖均为独立审查通过的精确 SHA。先检查 P2.5 实际公开接口，不根据本文旧函数形状批量实现。

### F0：锁定 SDK 群协议与最小接入方式

**依赖：** P2.5 Task 8 与本计划经审查。**结果：** §1.3 的能力在离线协议边界可复现，指出真实平台待验证项。**文件：** 本文/handoff；临时探针在工作树外，不改产品代码。

- [x] 锁定 SDK + 本机合成协议端点核实 raw 群事件：到达顺序、ack 时机、字段来源、mention 与 bot 身份（F0 证据 1–3）。
- [x] 公开 `get_chat_members` 的 force、分页、部分名单、权限/限流/超时与缓存行为（F0 证据 4）。
- [x] 公开发送的 `reply_to`、`reply_target_gone="fail"`、单次尝试、单条文本与结果分类（F0 证据 5）；发现并修复无 `code` HTTP 错误被记为已发送、原消息撤回/不可见被记为结果不明两处分类缺陷。
- [x] 记录 P3 所需平台权限与入群前提（F0 证据 7）；公开接口均可满足契约，无需另写鉴权/传输 SDK。
- [x] 独立审查：首轮在 `a455a6d` 提出 2 项阻断（错误码语义与投递分类、状态/I/O 事实未同步）及证据边界修订，处理后复审 `f6875f9` 通过、无阻断；进入 F1。F0 只证明锁版 SDK 在合成协议下的行为，不是平台验收。

**F0 证据（2026-10-04，起点 `e153cd9`）。** 环境：`lark-channel-sdk==1.4.0`、`websockets` 15.0.1、`httpx` 0.28.1、Python 3.11.16。探针在工作树外（`probe.py` sha256 `4f532e61…7a2ada`、`fake_feishu.py` sha256 `296de823…ac6b`）：本机 127.0.0.1 上的合成 OpenAPI（HTTP）与长连接端点（SDK 自带 protobuf 帧）；产品 `lark_channel()` 装配的 `ChannelConfig` 原样复用，只把 `domain` 换成该端点；入站经产品 `LarkTransport` 桥。全部身份、事件与响应均为合成，没有连接真实飞书、模型、StarRocks 或外部 MCP。替身能证明“锁版 SDK 收到这些协议输入时怎样处理”，不能证明真实平台实际发送的字段、投递、限流、权限审批与关闭行为。

1. **raw 到达与 ack。** 产品配置（`dm_policy/group_policy=disabled`、`emit_raw_events=True`、`include_raw=False`）下推送 9 帧：@本 bot、无 mention、@其他 bot、正文写“@小维”但无 mention、其他群、bot 发送者（`sender_type=app`）、其他租户、单聊对照、重复 `event_id`。9 帧全部到达产品 raw 桥，9 个 ack 均为 200，本机观测均在推送后约 1–6 ms 回写。锁版 `ws/client.py` 的 `_handle_data_frame` 同步调用分发器（只把处理排程到 SDK 后台循环）后立即写 ack，处理与 ack 在两个事件循环上并行、没有先后屏障：ack 不等待也不反映小维是否接受或落库（与 §1.3 的继承限制一致），“ack 先于 raw 回调”只是本机观测，不作保证。锁版 `channel.py` 的 `_handle_message_event` 第一步即 `_emit_raw_event`，早于 SDK 去重与 policy，因此重复事件也会到达，去重只能由小维完成（官方说明重复推送应按 `message_id` 而非 `event_id` 去重）。
2. **字段与 mention。** raw 字典来自 SDK 类型模型再序列化：header 有 `app_id/create_time/event_id/event_type/tenant_key/token`；sender 有 `sender_id.open_id/union_id`、`sender_type`、`tenant_key`；message 有 `chat_id/chat_type/content/message_id/message_type/create_time/mentions` 等；mention 只保留 `key`、`id.{open_id,union_id,user_id}`、`name`、`tenant_key`，官方的 `mentioned_type` 被模型丢弃。正文中的 `@_user_1` 与 `mentions[].key` 对应，因此只能按 `mentions[].id.open_id == 可信 bot open_id` 判断 @本 bot，再按其 `key` 删除正文 token；名称与纯文字“@小维”不可用。当前产品 `_parse` 对这些群事件一律拒绝（`chat_type`；bot 发送者为 `sender_type`；其他租户为 `tenant`），单聊对照被接受：现状不开放群入口。
3. **bot 身份。** 启动时 SDK 用应用凭据取租户 token 后请求 `GET /open-apis/bot/v3/info`，`get_bot_identity()` 返回其 `open_id` 与 `app_id`。该接口返回权限错误时，SDK 改查 application 接口后放弃并转入后台重试，通道仍为 ready，但 `get_bot_identity()` 抛出 `not_connected`：群入口必须在身份未解析时整体拒绝（可再与可信配置核对），不能退回按名称识别。
4. **成员目录。** `force=True` 每次访问 API（成员变化后 1 次请求取得新名单）；不带 force 时返回缓存（0 次请求，旧名单）。5 人、`page_size=2`、`max_pages=2`：2 次请求返回前 4 人，第 5 人不在结果中，返回值是普通 list、没有完整性标记；此后不带 force 的调用返回更早缓存的完整旧名单（0 次请求）。权限码 99991672 与 HTTP 403 → `permission_denied`，232011（bot 不在群）→ `unknown`，99991402 与 HTTP 429 → `rate_limited`，HTTP 500 非 JSON → `unknown`；全部抛出 `FeishuChannelError`，不回退缓存。SDK 期限取 `transport.http_timeout_seconds`（产品未设置，默认 30 s；调为 0.5 s 时 0.51 s 后 `send_timeout`），外层 `asyncio.timeout(0.5)` 也能按时结束等待。产品配置没有 `resolve_chat_members` hook。**F1/F2 约束：** 只用 `force=True`、固定页数与独立期限；找到 sender 才算成员证据（页数用尽时取到的部分名单中找到也算），未找到（含页数用尽后的部分名单中未找到）、任何异常或超时都按“无法确认”拒绝；不得配置 hook。同群并发查询会合并为同一次在途请求（源码 singleflight），视为同一时刻的事实。
5. **原消息回复。** `send(chat, {"text": …}, {"reply_to": …, "reply_target_gone": "fail"})` 发出 1 次 `POST /open-apis/im/v1/messages/{id}/reply`（body：`msg_type=text`、`content`、每次调用新生成的 `uuid`）。产品 `max_attempts=1`：5xx 业务码只发 1 次（结果不明），限流与 230003 为明确失败。错误码以官方回复接口文档为准、SDK 分类为辅：SDK 归为 `target_revoked` 的码 {230002, 230005, 230017, 230020} 中，230002 是 bot 不在群、230017 是 bot 不是资源所有者、230020 是**单群限频**（230005 未见于该文档）；它们在 `fail` 下明确失败、0 次新建，默认 `fresh` 会再新建 1 条无关联消息（230020 时等于把限频改发），所以必须显式选 `fail`。官方 230011（原消息已撤回）、230050（原消息对操作者不可见）被 SDK 归为 `unknown`；平台已拒绝、未发出，`send_outcome` 按 `raw_code` 记为明确失败，`fail` 下同样 0 次新建。以上分类由真实 SDK + loopback 回复端点回归覆盖（`test_real_sdk_reply_maps_target_codes_without_fresh_send`：成功、230011、230050、230020 各断言结果、1 次回复、0 次新建；修复前 230011/230050 两例失败，改用 `fresh` 时 230020 一例失败）；合成响应只证明 SDK 与产品分类，不证明真实平台何时返回这些码。正文恰为 `text_chunk_limit` 字时 1 次请求；多 1 字时第二段以**新建消息**发出，群回复必须保持渲染结果不超过该值（现有 `render` 已保证）。**缺陷与修复：** HTTP 500 `{}`、502 `{"msg":…}`、404 `{"msg":…}` 被 SDK 当作 code 0 的成功（无 `message_id`），产品原记为 `sent`，现有单聊发送同样受影响；本次改为成功必须带 `message_id`，否则 `unknown`，并以真实 SDK + loopback 端点的回归用例覆盖（`test_real_sdk_send_maps_http_outcomes_without_retry`，修复前 3 例失败）。
6. **入站附加 I/O 与生命周期。** SDK 规范化阶段会为每条非重复消息的发送者请求 `GET /open-apis/contact/v3/users/batch`（9 帧中 8 次，重复帧被 SDK 去重），发生在 policy 拒绝之前，单聊现状同样如此；产品已设的 `resolve_sender_names=False` 只关闭事后的姓名回填，挡不住这次请求（`lark_channel` 说明已据此更正）。公开构造参数 `FeishuChannel(name_lookup=...)` 传入不做 I/O 的函数后，3 条入站事件的附加请求为 0。**F2 约束：** 以该公开参数关闭姓名查询，避免任何群消息（含未 @、非指定群）消耗应用配额或依赖通讯录权限；本次不改单聊行为。一个进程只能运行一个长连接通道（WS 客户端绑定模块级事件循环）。SDK `stop()` 通常约 0.05 s。源码事实：`_sweep_bg_loop_tasks` 对后台循环残留任务固定等待 5 s + 1 s。实测（F0 探针，间歇）：完整场景下 12 次中有 4 次落入该等待（期限 30 s 时测得 6.0/6.01 s，期限 5 s 时 6 次中 2 次超时），短场景 7 次均正常；审查后复跑未再复现，触发条件未定位，不是稳定必现。产品 `stop_timeout_seconds` 小于约 6 s 时停机可能被报告为未干净退出；**F3 要求：** 启用群入口时配置校验拒绝小于 7 s 的 `stop_timeout_seconds`，并在 F3 drain/停机用例中断言；P3 用真实连接复核。
7. **P3 平台前提（官方文档核对，未在租户验证）。** 事件 `im.message.receive_v1` 收群内 @机器人消息需 `im:message.group_at_msg`（或其 readonly 变体），单聊为 `im:message.p2p_msg`；不需要“群内所有消息”。成员列表 `GET /open-apis/im/v1/chats/:chat_id/members` 需 `im:chat`、`im:chat:readonly`、`im:chat.members:read` 或 `im:chat.group_info:readonly` 之一，调用方必须在群内（否则 232011），`page_size` 至多 100，限频 50 次/秒；回复 `POST …/messages/:message_id/reply` 需 `im:message`、`im:message:send_as_bot` 或 `im:message:send` 之一，机器人须在群内且可发言，每群 5 QPS；关闭姓名查询后不需要通讯录权限。机器人入群、指定群规模（决定页数上限）、实际字段与限流在 P3 控制台与获准群中确认。

### F1：可信群身份、共享归属与存储闭环

**依赖：** F0。**结果：** 同群 A/B 共享会话和获准事实，所有入口仍按当前 actor 与 owner 复核。**文件：** models/channel/channel_store/session/evidence/runtime、下一应用迁移；对应现有测试及 `tests/sdk_core/test_group_identity.py`（需要 `tests/sdk_core` 的隔离 PostgreSQL fixture，因此不放在 `tests/p25`）。

- [x] 先在测试 PostgreSQL 保存 A 的请求/查询 Evidence，让 B 在同群回放/验证/读取；验证新查询保留 B 为 actor、旧事实保留 A 为采集者。无变化成功对照必须经过真 PolicySession/SQLAlchemySession。
- [x] 同一 actor 的私聊、Web、其他群/租户、不同 generation/ModelBinding 全部不能读群事实。伪造 owner、receipt、request_ref、同 ID 改 sender/chat/正文、旧个人记录混入群都拒绝；拒绝时模型/业务 SQL 为 0。
- [x] 检查 ownership/fingerprint 的全部调用者，包括 RequestStore 条件更新、Evidence record/read、最终提交、读取、resend、恢复/清理。添加最少的应用迁移与回复目的地字段，SDK 表不改。
- [x] 当前成员成功/失败/未知，排队后撤权与最终交付前撤权均覆盖；新配置不能把群权限赋予私聊。个人授权/历史/重发成功对照保留。
- [x] 运行 §5 G1；隔离变异去掉群隔离、沿用原采集者授权、抹去当前 actor、替换保存目的地分别应失败。提交 `feat: separate group conversation ownership from turn actors`（`e844554`）。
- [x] 针对精确 SHA 独立审查；通过后进入 F2。首轮针对 `16a1b08` 提出 3 组阻断（receipt 与授权结果未完整绑定原身份、失败回执的成员语义冲突、状态文档未同步），第二轮针对 `8698d37` 指出执行输入（正文、用途、会话、Context）未绑定数据库中的真实请求，均已修复；复审 `3140b76` 通过、无阻断，随 PR #44 合入 `0b1bdee`。

**F1 实施说明（2026-10-05，起点 `9495db6`，即 PR #43 的 F0 复审记录）。**

- **owner 与 actor。** `Owner(kind=personal|group, id)` 是会话、历史与证据的归属；`Identity.subject_id` 仍是本轮发起人，授权按它复核。个人身份省略 owner 时补为本人，个人路径的类型与行为不变。群 owner 的 `id` 是 `[app_id, tenant_key, chat_id]` 的 JSON 编码，换应用、租户或群即是另一个 owner。
- **存储 v5（`migrations/005_group_ownership.sql`）。** 会话与渠道映射的 `subject_id` 改名为 `owner_id` 并加 `owner_kind`；请求与证据加 `owner_kind/owner_id`，`subject_id` 保留为发起人/采集者；旧行全部回填为个人归属。`owner_kind` 不设默认值，写入必须显式给出。请求加 `reply_chat_id/reply_message_id`，约束保证群请求必有、个人请求必无；新增失败码 `access_denied`。SDK 表不改。
- **键与摘要。** 个人的会话语境、请求键与正文摘要保持 v4 的编码（升级后同一请求仍是重复请求，有测试证明）；群的编码另带 `"group"` 与群 owner，长度不同，不会与个人碰撞。群请求键只含群 owner、会话语境与原消息编号，**不含发起人**；正文摘要另含发起人，因此同一消息换人、换正文都是冲突。所有条件更新同时绑定 owner、发起人与轮次。
- **授权时机。** `AccessPolicy` 新增 `resolve_group(group, actor, verify_member=…)`：接受时 `verify_member=False`，只核对可信事件与指定群配置，不计算工具范围（`RequestReceipt` 不再携带执行 context）；`process` 开始运行前、`ResultDelivery` 每次读取与发送前各以 `verify_member=True` 查一次当前成员资格。**执行绑定数据库中的真实请求：** `process` 开始前按当前授权重新解析（个人与群都是；是否为群请求以记录的 owner 判断，群范围须与记录的群 owner、回复群一致，个人请求不得带群范围）；随后 `ChannelStore.start(record, message=, policy_version=)` 在同一事务内锁定请求行，核对身份键（渠道、请求键、owner、发起人、轮次）、会话语境、会话、用途、回复目的地，并按该行重算 `message_digest` 与正文比对，一致才 `accepted → running` 并返回数据库中的记录。内容不符（含接受后数据策略版本已变化）在同一事务记 `failed/access_denied`；身份键不符得到 `RequestUnavailableError`，不改动任何请求。执行 context 只由数据库记录、当前授权与配置预算生成，receipt 不能扩大工具或预算。个人请求因此也在开始时按当前授权重算工具范围（此前沿用接受时的 context）。个人授权结果的 subject 必须等于请求的 subject。开始时不能确认记为 `failed/access_denied`（模型与工具均为 0；固定回执同样在发送前查成员，确认后才发送）；交付前不能确认时不发送、不取得投递权，与个人路径交付前撤权的既有残留一致（请求保持 `completed+pending`，重启恢复记为 failed，显式重发重新授权）。`authorize` 不查成员目录：同一轮内的工具与证据复核沿用本轮已确认的成员资格，只核对群 owner、群工具与目标。读取共享事实按当前读者授权，不按采集者。
- **产品 `StaticAccess`。** 可选 `GroupAccess(scope, tools, members)`：全员同权、可用全部已配置目标；`members(chat_id, open_id)` 只有返回 `True` 才算成员，异常、`None`、未找到都拒绝。群授权不进入个人 `resolve`/`authorize`，个人授权也不进入群。
- **留给 F2。** 配置项（指定群、群工具、成员查询页数与期限）、用 SDK `get_chat_members(force=True)` 实现 `members`、群入口解析与原消息发送、群请求的显式重发（当前个人 `requests resend` 定位不到群请求，失败方向安全）。F1 的成员目录只是测试替身。

**F1 证据（离线；隔离 PostgreSQL、真 Runner + 脚本模型、真 PolicySession/SQLAlchemySession、产品 `StaticAccess`）。** `tests/sdk_core/test_group_identity.py` 38 项（第二轮复审修复后）：A 查询 → B 同群追问并新查（同一 Session，B 的回答引用 A 的事实；证据分别记 A、B 为采集者，Session 与映射只记群 owner）；读取按当前读者授权（撤下群工具后 B 读不到）；A 离群后 B 仍可追问、B 离群后被拒（按当前 actor，不按采集者）；同名个人身份的私聊/Web、其他群/租户/应用读不到群证据，个人渠道引用群证据的回答不交付；新 generation 读不到上一代；纯澄清历史的群 Session 不能被个人/其他群身份打开，换运行绑定也拒绝；同聊天同消息编号的私聊记录与群互不混用；群请求保存回复目的地，同消息换人/换正文冲突，重复请求不再运行；群信封不能指向其他会话或渠道；他人、个人身份或省略群都不能借用 A 的请求；未配置的群/租户/应用在任何状态前拒绝；接受不查成员目录、开始时查一次、同轮工具不再查；排队后离群/目录异常/结果不明均 `access_denied` 且模型与工具为 0；交付前离群不发送，开始前被拒的请求在成员持续离群或目录异常/结果不明时同样不发送回执、不取得投递权（保持 `failed+pending`）；群诊断请求的 receipt 单独或组合换正文、把用途改为查询、换会话、换回复消息或回复群、去掉或换掉群，都在 Runner 前记 `access_denied`（模型、Adapter 与原会话/目标会话的 SDK Session 条目均不增加，请求行保持原会话与用途）；记录换发起人或轮次时不运行、请求保持 accepted，原样 receipt 随后照常运行一次、重复事件不再运行；同时有个人授权的 A 去掉群范围也不能改按个人授权运行；个人诊断请求换用途、正文、会话、加群范围或接受后数据策略变化同样在 Runner 前拒绝；授权来源把 mallory 解析为自洽的 alice 决策时接受即拒绝（无请求行、模型与 Adapter 为 0），mallory 也不能读取或发送 alice 的结果；群授权不开放私聊/Web，个人授权照常；恢复中断群请求并以群 owner 关闭会话；v4 个人请求升级后键与会话不变。隔离变异 12 项（证据忽略 owner、开始时沿用接受时 context、抹去采集者、替换回复目的地、群请求键含发起人、个人摘要改编码、`authorize` 不核对群、Session 认领不核对 owner、读取不核对发起人、交付前不查成员、接受时查目录、群工具泄入个人授权）均使对应用例失败；第二轮修复后另加 7 项，均被捕获：`start` 不核对绑定、只核对字段不核对正文摘要、只核对正文摘要不核对会话/用途/回复目的地、`start` 不核对身份键、个人请求不拒绝群范围、个人决策恢复为只核对自洽、以 receipt 的群范围（而非记录的 owner）判断是否为群请求。原 12 项中“开始时沿用接受时 context”所针对的代码已删除。回归：`tests/sdk_core tests/p1b tests/p25 -W error` 1924 passed、39 deselected（首轮复审修复后 1933 passed，第二轮修复后 1942 passed，均 39 deselected）；为适配新列，原有用例只改了版本号与按位置插入的 SQL（改为列名），断言未放宽。

### F2：指定群 @ 入口与原消息交付

**依赖：** F1。**结果：** 一个指定群可经正式链路进入 P2.5 Agent，严格按原消息交付，Web/单聊语义保留。**文件：** feishu/config/runtime/channel/cli；`test_feishu`、新增 `tests/sdk_core/test_group_gateway.py`（需要 `tests/sdk_core` 的隔离 PostgreSQL fixture，理由同 F1，因此不放在 `tests/p25`）。

- [x] raw 事件 → LarkTransport → Gateway → ChannelService → 真 Runner/测试存储 → ResultDelivery → SDK 协议替身的成功链，不能只测试构造 InboundRequest。
- [x] 未 @、@其他 bot、仅正文伪造 @、非指定群/tenant、机器人、附件/超长/超龄事件均无 Agent/模型/DB；保留四项身份，body 不能覆盖它们。群成员未配单聊 users 仍可在指定群使用。
- [x] 沿用 P2.5 自然语言及快捷命令；固定样例中的诊断/生成/模糊请求应由 Agent 不调用查询工具；显式诊断入口确定性拒绝查询，当前 actor 与有界作者标识进入正确的历史/模型位置。`/新建` 的群语义有提示。（离线只证明工具集合与门控；Agent 对群内自然语言的判断沿用 P2.5 样例，真实模型在 P3。）
- [x] 同 message_id 重投、并发重投、发送失败/未知/原消息删除、目的地篡改与显式重发均验证；事实/分析/来源截断保持 P2 规则。排队通知属于 F3（F2 不发排队通知），该项随 F3 验证。
- [x] 运行 §5 G2；变异跳过 mention/当前身份/原消息绑定、允许 SDK fallback 或重复执行应失败。提交 `feat: admit mentioned group messages and bind replies to their origin`；首轮独立审查（`69e6a02`）的阻断已修复，复审 `200acd3` 通过，随 PR #45 合入 `6168c97`。

**F2 实施说明（2026-10-05，起点 `0b1bdee`，即 PR #44 合入后的 main）。**

- **配置。** `feishu.group`：`chat_id`（`oc_` 开头）、`tools`（全员同权，须为已登记的 StarRocks 工具，由运行配置核对）、`member_page_size`（≤100）、`member_max_pages`（≤20）、`member_timeout_seconds`（≤30）。未配置时群消息一律按 `chat_type` 丢弃，单聊不变。
- **入口。** `_parse` 先核对事件类型、应用、租户与 `sender_type`，再按 `chat_type` 分支：单聊照旧经 `users` 映射；`group` 须是配置群、发送者 `open_id` 合法，actor 是该 `open_id`、owner 是群，不查 `users`。随后核对消息类型、时效与正文，最后要求平台 `mentions[].id.open_id` 等于本机器人：身份来自 `LarkTransport.bot_open_id()`（SDK `get_bot_identity()`，`app_id` 须等于配置、`open_id` 须合法，否则为 None，群消息以 `bot_identity` 丢弃）。只删除本机器人 mention 的 key（按词边界，`@_user_1` 不吃掉 `@_user_10`），其他 mention token 原样保留。原因码：`chat`、`mention`、`bot_identity`、`sender`、`chat_type` 等，日志不含正文与标识。
- **作者标识。** 交给 `ChannelService` 的群消息为 `【群成员 <sha256(open_id) 前 8 位>】\n正文`，正文中同样的开头改写为 `[群成员 `，首行之外不能冒充；标识进入请求摘要（重投得到相同摘要）、SDK Session（历史中每轮保留作者）与输入上限，不含 `open_id`，不赋予权限。没有另建历史表或改 SDK 输入格式。
- **交付。** 群内一切发送（最终结果、固定回执、空命令提示、`/新建` 提示）都以 `reply_to=原消息`、`reply_target_gone="fail"` 单次发送；`ResultDelivery.send` 对群请求另核对调用方的群与原消息等于接受时保存的回复目的地（按键构造本应一致，作为纵深防线）。`LarkTransport.send` 把 `<at` 改为全角（单聊同样），回答与工具结果不能 @ 任何人（含 `user_id="all"`），字符数不变。
- **成员目录。** `LarkTransport.is_member` 调 SDK `get_chat_members(chat_id, page_size, max_pages, id_type="open_id", force=True)`，外层期限 `member_timeout_seconds`，超时取消 SDK 循环上的查询；找到发送者才为 True，未找到、返回非列表为 False，异常与超时原样抛出（授权方拒绝）。`serve` 先装配 SDK 通道再装配运行对象，`StaticAccess` 的群策略绑定这一个通道；`open_runtime` 未给 `members` 时群授权一律不成立。F1 的调用时机不变：开始运行一次、每次发送前一次，同轮工具复核不查。
- **入站附加 I/O。** `lark_channel` 以公开参数 `name_lookup` 传入不做 I/O 的函数，任何入站消息都不再请求通讯录（单聊同样，F0 证据 6）。
- **`/新建`。** 群内按群轮换（F1 的 `new_session(group=…)`，须当前成员），提示“已为本群新建会话”；非成员不轮换、不提示。运行中拒绝沿用现有规则，排队中拒绝属于 F3。
- **显式重发。** `runtime.resend(..., group=True, subject_id=<发起人 open_id>, message_id=…)`，CLI `requests resend --group` 与 `--chat` 互斥：群取自配置，只回复保存的原消息，发送前经 SDK 查一次成员；同时给 `chat_id`、未配置群或单聊缺 `chat_id` 均为 `ResendTargetError`；单聊路径定位不到群请求，他人（即使是成员）定位不到 A 的请求。
- **行为变化（单聊）。** 入站不再请求通讯录；发出文本中的 `<at` 改为全角；`resend` 的 `chat_id` 改为关键字可选参数。其余单聊、Web 语义不变（既有用例原断言通过；`test_feishu` 的发送替身只增加 `reply_to` 参数）。
- **留给 F3。** 群 FIFO、排队提示、排队中 `/新建` 拒绝与 `stop_timeout_seconds ≥ 7`。F2 中同群并发：`consumer_count=1` 时后接受的一条在全局队列中等待，按接受顺序运行；大于 1 时同群的并发请求相互竞争，同一时刻最多一条进入共享 Session，其余记为 `failed/busy`。F2 不保证接受顺序：先接受的一条若在开始前的成员查询中较慢，可能是它记为 busy、后接受的一条运行（审查反例）。先到先运行由 F3 的群 FIFO 保证。

**F2 证据（离线；隔离 PostgreSQL、真 Runner + 脚本模型、真 PolicySession/SQLAlchemySession、产品 `StaticAccess`）。** `tests/sdk_core/test_group_gateway.py` 48 项：未在单聊名单中的成员 @小维 → 一轮查询、一条回复原消息（`reply_to` 为原消息）、成员目录恰好查 2 次（开始、发送前）；A 问、B 追问时 B 的模型输入回放 A 带标识的提问，两人标识不同且不含 `open_id`；正文伪造首行被改写、身份仍为 A；只删本机器人 token；`@小维 /诊断` 隐藏查询工具并记为 diagnose；只 @ 无正文时一条提示、重投不重复、无请求行；`/新建` 由成员 B 轮换全群、非成员 C 不轮换，之后 A 的提问不带历史；14 类入站拒绝（无 mention、mentions 缺失、@其他 bot、只写“@小维”、其他群/租户/应用、发送者租户不同、bot 发送者、非法发送者、话题群、图片、超龄、超长）以及机器人身份未解析、未配置群，均无请求行、模型、工具、成员查询与发送；A 的单聊消息仍被丢弃；三路并发重投加一次顺序重投只运行一次、回复一次，成员目录仍只查 2 次；accepted、running 与 sending 时重投不查目录、不发送；completed/pending（发送前离群）与 failed/pending（开始前不是成员）在成员复核通过后重投各发送一次、回复原消息；回复 failed/unknown/异常后的重投不查目录；同消息换发送者冲突；回复 failed/unknown/异常各记一次且重投不重发；非成员 → 模型 0、`failed/pending` 且不发送；运行中离群 → 不发送。传输：群回复参数为 `reply_to`+`fail`、`<at`（含大小写与空白变体）改为全角且长度不变、单聊参数不变；机器人身份只接受本应用且格式合法；成员查询 5 次调用参数全部为 `force=True`、配置页数与 `open_id`，未找到/非列表为 False、权限错误抛出、超时在期限内抛出并取消 SDK 上的查询；群配置上下界。正式入口：`runtime.serve` + SDK 公开面替身中 A 得到原消息回复（回答里的 `<at user_id="all">` 已中和）、C 的请求 `failed/pending` 且不发送，成员查询共 4 次；群工具未登记时配置拒绝；群 owner 规范编码恰为 200 字符时配置成功，超出 1 字符（及因 JSON 转义变长）时 `ServeConfig`、`FeishuConfig` 与 `load_config` 均拒绝，错误不含应用、租户或群标识；`runtime.resend` 群路径在发起人离群、走单聊路径时拒绝且零发送，成功一次后不再发送，回复参数为原消息、无业务 SQL；B 定位不到 A 的请求；目标参数与配置不符时拒绝。真实 SDK：`runtime.serve` 用产品 `lark_channel` 装配的真实 `FeishuChannel`（只把域名换成本机合成 OpenAPI、传输改为 webhook），机器人身份由 SDK 启动时的 `bot/v3/info` 取得；@ 事件经 SDK 分发器进入正式装配，成员接口恰好请求 2 次，回复发往 `/im/v1/messages/om_real/reply`，未 @ 的事件不运行，没有通讯录请求、没有新建消息请求。隔离变异 16 项（跳过 mention、丢失群身份、不回复原消息、允许 SDK 改发、入站姓名查询未关闭、机器人身份不核对应用、成员查询走缓存、不中和 `<at`、正文可冒充作者行、删除 token 不看边界、机器人身份未解析时放行、群重发按个人路径、群发送者须在单聊名单、群消息不加作者标识、不核对指定群）中 15 项在 `69e6a02` 上被捕获；“网关对重投也入队”在当时存活，曾被记为等价变异，审查指出这一结论不成立：它不重复执行 Agent/SQL，却会改变外部 I/O（每次重投多一次 `force=True` 成员查询），见下方审查修复。回归：`tests/sdk_core tests/p1b tests/p25 -W error` 1986 passed、39 deselected；G2 311 passed；Web 浏览器场景 3 passed；文档检查 8 passed；ruff、mypy、`uv lock --check`、`git diff --check` 通过；原有用例的断言未改动。

**F2 首轮独立审查修复（审查版本 `69e6a02`）。** 三组阻断均成立。

- **A 重投放大成员目录查询。** 根因：网关对所有 `created=False` 的重投都调用 `_deliver`，而 `ResultDelivery.send` 先做当前授权（群请求即一次成员查询）再判断状态，于是排队、运行、发送中或投递已落定的请求每次重投都多一次 `force=True` 查询（3 路并发 + 1 次顺序重投共 5 次，正常为 2 次）；锁版 SDK 只合并同一时刻的在途查询，顺序重投仍会请求。修复在可信网关边界按 `receipt.record` 预筛：只有 completed/failed/interrupted 且投递为 pending 的重投进入首次发送竞争，其余直接返回。`ResultDelivery` 的授权顺序不变（先授权后读取），预筛只减少 I/O，不替代交付时的当前权限、成员、证据与目的地复核；记录在接受后变化时仍由 `send` 的投递权竞争决定。
- **B 群 owner 编码超限。** 根因：配置字段各自有上限，组合后的规范 owner（紧凑 JSON 数组）可超过 `Owner.id` 的 200 字符，群授权解析时才失败，所有群请求都被当作权限拒绝。修复把编码收拢为 `models.group_owner`（`GroupScope.owner` 调用它，编码不变，F1 已落库的归属键不变），`FeishuConfig` 在加载时用它校验，超限给固定说明；群入口与群重发都只接受同一份 `load_config` 结果。
- **C 文案过强。** 部分名单中找到 sender 是本次成员证据，未找到只是无法确认（§1.3、§2.1、F0 证据 4、§7、`MemberCheck` 注释已统一）；`consumer_count>1` 时 F2 不保证先接受者运行（见“留给 F3”）。

修复验证：修复前新增与加强的 8 项用例全部失败（重投各多查 1–3 次目录、超限配置被接受）；修复后通过。隔离变异 7 项全部捕获：重投一律投递（修复前行为）、重投也入队（原“等价变异”）、预筛不看请求状态、预筛不看投递状态、重投一律不投递、取消 owner 配置校验、按原始长度估算 owner。回归：`tests/sdk_core tests/p1b tests/p25 -W error` 1990 passed、39 deselected；G2 315 passed；Web 浏览器场景 3 passed；文档检查 8 passed；ruff、mypy、`uv lock --check`、`git diff --check` 通过；原有用例只在重投与发送失败用例中增加成员查询次数断言。

### F3：同群有界串行与不同会话并发

**依赖：** F2。**结果：** 同时 @ 有可观察的先后和限额，队列不会变成无界积压或阻塞全部会话。**文件：** feishu/channel_store/config/runtime；现有生命周期用例及 `tests/sdk_core/test_group_queue.py`（与 F1/F2 同理放在 sdk_core，需要其隔离 PostgreSQL fixture）。

- [x] 用事件屏障控制 A/B/C 接受/开始/结束，断言按接受顺序、同群最大 active=1；B 开始时可见 A 已提交的历史。另一个单聊/Web 同时能运行，总 active 不超过原全局上限。
- [x] 队列长度等于/超过上限、等待刚好/超过期限、前一轮超时/异常、通知失败、重复事件、排队中 actor 离群、存储失败和实例锁丢失均有成功/拒绝对照；到期失败无需等待整个长任务结束。
- [x] 澄清释放队头、后续成员可问新问题；`/新建` 在运行/排队时拒绝、空闲可轮换。群跨目标也不并发读写一个 Session。
- [x] 启用群入口时 `stop_timeout_seconds` 小于 7 s 的配置被拒绝（F0 证据 6：SDK 关闭可能固定等待 5+1 s），7 s 及以上通过；单聊配置的现有取值保持不变。
- [x] drain 包含落库前 receive、群 FIFO、普通队列和在途发送；停机期限、取消、重启恢复不重放 SQL。真实 PostgreSQL 对照：只有 accepted 的群请求 → 请求 interrupted、原 Session 保持可用；有 running 或 Session writing/提交不明 → 原规则关闭。同群一个 running 加多个 accepted 仍关闭，不能因存在排队项就重新开放。旧单聊/Web 恢复保持原行为。
- [x] 对接受→入队、出队→持久 start、start→SDK 首调用及实例接管逐点设屏障；只有持久化 start 成功后才允许 Runner/Session 写入，恢复读取中断前状态与更新/关闭必须在同一所有权事务内完成。取消/存储不明不得归为“仅排队”。
- [x] 成员目录调用计数：正常轮仅出队开始一次、发送前一次；同轮多工具不会增加目录查询，单独重发发送前重新查一次。未 @/其他群无目录 I/O；只有一次无数据排队提示不查询成员目录。
- [x] 运行 §5 G3；隔离变异去掉同群串行、容量/期限、出队重验、把 accepted 一律关闭群 Session、把 running 误当仅排队，或让等待者占 worker 槽应失败。提交 `feat: queue group turns within bounded channel concurrency`；三轮独立审查（`0b92b90`、`a3c6f41`、`4c7ed17`）的阻断已修复，复审 `8e1ace2` 通过、无阻断，随 PR #46 合入 `1178c74`。

**F3 实施说明（2026-10-05，起点 `6168c97`，即 PR #45 合入后的 main）。**

- **群 FIFO。** 复用网关现有的有界队列与固定消费者。指定群同一时刻只运行一条；尚未开始的群请求（含下一条将运行的队头）都留在进程内有界 `deque` 中，不占消费者、不创建等待协程，始终受等待期限管理（第二轮审查 B）。群空闲且队头的排队提示尝试已结束时，全局队列中放入一个“群可运行”标记（同一时刻最多一个，占为群另留的保留槽：`maxsize = queue_size + 1`，其他请求按计数仍以 `queue_size` 为上限），消费者取到标记时在同一步内（无 `await`）核对期限、取出队头并标记运行中；标记已无可运行队头时直接丢弃。群请求的持久接受与入 FIFO 在同一个 `asyncio.Lock` 临界区内完成，接受之后到入 FIFO 之间没有 `await`，运行顺序就是持久接受的顺序（不按飞书 `create_time`）。队头的 `process` 与一次投递落定后（失败、澄清、模型失败都算结束）才放下一条的标记，并在 `task_done` 之前完成；`receive` 都结束后排队提示都已结束，群空闲且队头可运行时必有标记在队列中，`drain` 的 `join` 因此覆盖整个群 FIFO。`consumer_count>1` 时 F2 的“同群竞争、可能后到先跑”由此消除。
- **排队提示。** 只有新接受并进入等待的请求发一次无数据提示“已收到，前面还有问题在处理，将按顺序回复”（回复原消息），不经 `ResultDelivery`、不查成员目录、不占最终结果的投递状态；失败或结果不明只记日志，不重试。提示在群锁之外发送；等待项带一个“提示尝试已结束”标记（成功、明确失败、结果不明、异常或取消都算结束），标记设置前这条请求不交给消费者（等提示不占消费者，第二轮审查 B）、它的繁忙回执也不发送：本地的发送尝试中提示在前（首轮审查 B1）。这只是本地发送尝试的先后，平台实际收到与显示的先后仍受网络、发送期限与结果不明影响，提示发送超时后仍可能晚到。提示失败只是放行，不让请求永久阻塞；结果不明时不当作平台未收到，也不重发。重投经 F2 的预筛直接返回，不重复提示、不重新排队。
- **上限与期限。** 新增必填 `group.max_waiting`（≤100 且不超过 `queue_size`）、`group.max_wait_seconds`（须短于请求保留期）与 `group.wait_check_seconds`（≤60 且不超过 `max_wait_seconds`）。等待超过上限、或从接受（`created_at`，重投不刷新）起等待超过期限的请求记为 `failed/busy`，按原消息发送固定回执（发送前照常复核成员），不运行、不调用模型或业务数据库。到期分两步（首轮审查 B2）：`run()` 内的定时检查每 `wait_check_seconds` 取出群 FIFO 中**全部**已到期、尚未开始的请求（含等消费者的队头与提示仍在发送的等待项），消费者开始前再核对一次；两个入口都按 `turn_id` 登记后交给同一条路径（第二轮审查 A）：在 `run` 的任务组中先持久化为 `failed/busy`（只写本地存储，不等任何外部 I/O），再等该项的排队提示尝试结束，最后在最多 4 路并发内经 `ResultDelivery` 发送回执（投递权在提示结束后才取得；照常复核当前成员、接收权限、Evidence 与回复目的地）。登记期间同一请求的重投直接返回，不查成员目录、不发送；任务结束后仍为 `failed/pending` 的（例如成员资格暂时无法确认），重投照常按 F2 进入首次发送竞争。一条的成员查询或发送挂起、失败只占它自己的名额；`max_waiting` 只不计正在运行的一条：尚未开始的请求（含等消费者的队头）与回执落定前的到期项都计入，因此到期任务数不超过 `max_waiting`，新到的请求在容量满时（即使群已空闲）记为繁忙（第三轮审查）。`wait_check_seconds` 只约束发现与状态结算，平台实际收到回执另受成员查询、网络与发送期限影响。**契约差异：** 满与到期沿用现有失败码 `busy`（回执“系统繁忙，本轮未执行，请稍后用新消息重试”），不新增失败码，因而不改存储约束。
- **出队重验。** 沿用 F1/F2：`ChannelService.process` 在开始前按记录的 owner 复核当前成员与授权，排队中离群 → `failed/access_denied`、模型 0 次。存储或实例所有权故障锁低 readiness 后，队头会话保持封闭，后续出队项记为 `failed/busy`、不运行，发送因 readiness 锁低不取得投递权。
- **`/新建`。** 无需新代码：存储的轮换条件已拒绝会话中仍有 `accepted/running` 请求的轮换，排队项属于该会话，因此排队与运行中都拒绝，空闲时轮换。
- **停机。** 启用群时 `stop_timeout_seconds < 7` 在配置加载时拒绝（F0 证据 6），单聊取值不变。`drain` 依次等待已进入 `receive` 的调用（含排队提示的发送尝试）、全局队列（含群 FIFO 交接）与全部到期项的结算和回执任务；超时或 `run()` 被取消时仍有排队项或未落定的到期项则锁低 readiness。消费者在已收到取消请求时不再交接下一条、不再创建到期任务、不再取下一项：下层把取消转换成普通失败时（首轮审查修复中发现的原有缺陷；第二轮审查 C 补上“不交接”），消费者锁低 readiness 并结束，`run()` 不会挂住停机，也不向正在关闭的任务组创建任务。
- **恢复。** `recover` 在持有实例锁的同一事务内以 CTE 取得中断前状态：群会话中被中断的请求全部只是 `accepted`（从未持久启动），且会话元数据不在 `writing` 时，会话保持原状态；同会话有 `running`、会话处于 `writing`，或个人会话（单聊/Web），照原规则关闭。只有持久化 `start` 成功后才有 Runner/Session 写入（F1 的 `start` 绑定），所以“只在排队”不会把已开始的轮次误判为安全。
- **未新增的屏障。** 接受→入队之间没有 `await`，不存在可取消的中间点；出队→持久 `start`、`start`→SDK 首调用与实例接管沿用 F1 已有的绑定与屏障用例，本切片没有逐点新增屏障测试。群跨两个集群仍是同一个群会话，由同一 FIFO 串行，没有单独的跨目标并发用例。

**F3 证据（离线；隔离 PostgreSQL、真 Runner + 脚本模型、真 PolicySession/SQLAlchemySession、产品 `StaticAccess`；`run_turn` 外只包一层挂起与计数）。** `tests/sdk_core/test_group_queue.py` 首版 21 项（首轮审查修复后 32 项、第二轮后 48 项、第三轮后 49 项，见下）：A 运行中 B、C 先后到达 → 各一次排队提示，按接受顺序运行，同群同时最多 1 轮，B 的模型输入回放 A 的一轮；同时另一单聊会话照常运行（两个消费者，总并发 2）；成员目录每轮恰好 2 次、提示 0 次。等待上限 1：B 等待（恰好到上限），C → `failed/busy` 并回复原消息、模型 0 次。等待恰好 60 s 不结束、61 s 在检查间隔内结算为 `failed/busy`、随后回复，不等长任务；检查间隔很长时过期项在消费者开始前结算，下一条照常运行，重投不刷新期限、不再提示。队头 `TurnError` → failed 并回执，下一条继续；澄清释放队头；排队中离群 → `failed/access_denied`、模型 0 次、回执不发送，下一条继续；排队提示发送异常不影响后续交付；等待中的事件并发重投 3 次不重复入队或提示；存储故障时等待项不运行。`/新建` 在运行与排队时拒绝，空闲后轮换到第 2 代，排队项留在原会话。配置：上限与期限的上下界、`max_waiting ≤ queue_size`、`wait_check_seconds ≤ max_wait_seconds`、`max_wait_seconds` 短于请求保留期、启用群时 `stop_timeout_seconds` 6.9 拒绝 7 通过、单聊 5 仍通过。停机：drain 等待群 FIFO 跑完；队列已空但到期回执仍在发送时 drain 等它们全部落定（审查修复后为两条并发回执）；有等待项时 drain 超时锁低 readiness。恢复（真实 PostgreSQL）：只有排队项 → 请求 interrupted、群会话仍 `active` 且续问回放上一轮；同会话另有 running、会话处于 writing、个人会话只有 accepted → 照原规则关闭。正式入口：`runtime.serve` + SDK 公开面替身中 A 被接受后 B 排队（一次提示、随后按序回答），等待上限 1 时 C 记为繁忙、模型 0 次，成员查询共 5 次。F2 的 `test_group_gateway.py` 群配置辅助补新字段与 7 s 停机期限；一处 F2 用例因 F3 新增排队提示，把“第一条发送挂起”改为“第一条结果发送挂起”，并在断言中计入这条提示。

**隔离变异 14 项全部捕获：** 去掉同群串行、去掉等待上限、去掉等待期限、不做定时到期检查、出队不重验成员、队头结束不交接、排队不发提示、`accepted` 一律关闭群会话、`running` 误当仅排队、写入中会话不关闭、个人会话也保留、drain 不等到期回执（首轮存活，补“到期回执在途时 drain”用例后捕获）、去掉群停机下限、让等待项占用消费者。首版回归：`tests/sdk_core tests/p1b tests/p25 -W error` 2011 passed、39 deselected；G3 336 passed。

**首轮独立审查修复（审查版本 `0b92b90`）。** 两项阻断均成立，共同根因是群队列的状态推进与用户通知的发送生命周期耦合不正确：B1 交接只看等待项是否在 FIFO 中，不看它的排队提示是否发完，B 可能在提示发出前进入 Runner、先得到最终回答；B2 到期检查对每个到期项串行执行“结算 → 成员复核 → 发送”，第一项的成员查询或发送阻塞时，后续已到期项一直是 `accepted`。修复（`src/xiaowei/feishu.py`）：等待项带排队提示的完成标记（`_Job.notice`，`receive` 中在群锁之外发送，`finally` 设置），消费者与到期回执都先等它；到期检查与交接只把到期项从 FIFO 取出（同步、不等 I/O），交给 `run` 任务组中的独立任务先持久化为 `failed/busy`，再在 4 路并发内经 `ResultDelivery` 回执，回执落定前仍计入 `max_waiting`；`drain` 等全部到期任务。另修复一项修复中发现的原有缺陷：下层把取消转换成普通失败时，消费者曾继续等下一项，`run()` 无法结束；现在消费者在已收到取消请求时锁低 readiness 并结束。新增与调整的用例（`test_group_queue.py` 共 32 项）：提示挂起时 A 结束 → B 不进入 Runner、保持 `accepted`，放行后才运行且提示先于回答；提示挂起后明确失败、结果不明或抛出 → B 照常运行；提示发送中等待到期 → 检查间隔内结算为 `failed`，回执等提示结束后才发；drain 等提示尝试结束；B、C、D 同时到期而 B 的回执发送挂起、成员查询挂起或抛出 → 三条都在 1 s 内结算，C、D 照常回执，B 解除后按当前成员交付（抛出时不发送），三条的模型、Runner 与业务查询均为 0；6 条同时到期且回执全部挂起 → 全部结算，同时在途回执峰值 4，期间新到请求按容量满记为繁忙、不提示；下层转换取消时 `run()` 在 5 s 内结束并锁低 readiness。修复前（`4fe81e4`、`735ac05`）新增用例中 7 项失败，其余为对照；首轮提交的两处预期在修复中更正：发送挂起时 B 已取得投递权，状态是 `failed/sending` 而非 `pending`；并发回执之间不保证先后。

**修复的隔离变异 12 项，11 项捕获：** 消费者不等排队提示、提示结束不放行、到期回执不等提示、结算等提示之后才写、扫描串行等待回执（修复前行为）、回执不限并发、结算也占并发名额、等待容量不计未落定回执、drain 不等到期回执、到期回执绕过 `ResultDelivery` 授权、消费者吞掉被转换的取消。存活 1 项：到期扫描只看队首（修复前写法）。群 FIFO 按持久接受顺序排列、等待期限从接受时间起算，接受时间单调时两者等价；只在时钟回拨时有差别，未补用例。

回归（修复版本）：`tests/sdk_core tests/p1b tests/p25 -W error` 2022 passed、39 deselected；G3 347 passed；Web 浏览器场景 3 passed；文档检查 8 passed；ruff、mypy、`uv lock --check`、`git diff --check` 通过。

**第二轮独立审查修复（审查版本 `a3c6f41`）。** 审查的 A、B 两组阻断与 C、D 两项非阻断均成立，8 项独立诊断用例在 `a3c6f41` 上按所述原因失败。
- **A 根因：** 提示门与 4 路回执上限只绑定在单个 `_Job` 与 `_settle_expired` 上；重投重新创建 `_Job`，出队时到期又直接调用 `_deliver`，可以绕过原请求正在进行的到期处理。结果是回执先于提示、重投放大成员查询、并发超过 4。
- **B 根因：** 只有 `_group_waiting` 受定时检查管理，下一条一旦被提前移入共享队列，就脱离期限管理，并在消费者内等提示、占住消费者。
- **C：** 下层转换取消后，消费者仍会交接下一条，并向正在关闭的任务组创建到期任务。
- **D：** 配置与注释把本地结算时间和本地发送先后写成了平台上的承诺。

修复（`src/xiaowei/feishu.py`）：
- 尚未开始的群请求一律留在群 FIFO 中，共享队列只放“群可运行”标记（`_signal`）。标记在群空闲且队头提示尝试已结束时放入；消费者取到后由 `_start_group` 同步核对期限并取出队头。
- 到期项按 `turn_id` 登记（`_expiring`），定时检查与开始前核对都经 `_retire` → `_settle_expired`，删去旁路的 `_expire`。登记期间重投直接返回。
- 消费者在取消到达后不交接下一条。
- 文案统一为“检查间隔只约束发现与状态结算、先后只就本地发送尝试而言”（`config.py`、`feishu.py`、本节、README）。

新增 16 项用例（共 48 项），先提交审查反例对应的回归用例（`79edfc1`）。修复前其中 8 项失败、4 项为对照；修复后补 4 项，覆盖本轮首次变异中存活的行为。用例覆盖：
- B 的提示仍在发送时到期后被重投：不发回执、不查目录，提示结束后只回复一次；
- 到期任务的成员查询挂起时重投 3 次：成员目录仍只查 1 次，回执 1 次；
- 6 条回执中 4 条在途时全部重投：峰值仍为 4，重投不查目录；
- 唯一消费者被单聊占用时，群队头与 4 条等待都按期限结算，回执峰值 4，单聊结束后不运行过期项；
- 到期回执因成员无法确认而 `failed/pending`：原任务结束后成员恢复，重投复核并回复一次，之后不再查目录；
- 提示结束与到期同时发生（开始前核对、定时检查两种）：只结算一次、不运行；
- 已开始运行后超过期限：不结算；
- A 结束时 B 的提示仍在发送：B 照常到期，回执等提示；
- `consumer_count=1` 时等待提示不阻塞单聊；
- 等消费者的群队头到期只回复一次，之后新到的群请求正常运行一次；
- 开始前发现的到期同样登记去重；
- 提示一结束即运行（不等检查间隔）；
- 不可运行的队头到期后下一条立即运行；
- 等消费者的队头占“正在运行的一条”的容量（第三轮审查认定与契约不符，已反转，见下）；
- 取消被转换时 `run()` 以取消结束，不创建任务，B 留给恢复。

模型、SDK Session（`agent_messages`）与业务 Adapter 在这些拒绝/到期路径中均为 0 次。为使修复前的失败有界、可读，`settled` 超时改为报告当前状态，两处重投改为后台任务。一项对照（成员恢复后重投）把“等原任务结束”改为“反复重投直到交付”，以精确计数断言。

**第二轮修复的隔离变异 16 项全部捕获**（首次 12 项捕获、4 项存活，补 4 项用例后全部捕获）：
- 重投不看到期登记；
- 登记在结算后即释放；
- 开始前到期绕过统一路径（补用例后捕获）；
- 去掉统一回执信号量；
- 提前移入消费者（不等提示就放标记）；
- 不扫描等消费者的队头；
- 开始前到期仍运行（双重结算）；
- 开始前不核对期限；
- 运行中的请求留在 FIFO；
- 提示结束不放标记（补用例后捕获，原先被定时检查掩盖）；
- 定时检查后不放标记（同上）；
- 取消到达后仍交接；
- 容量不计待开始的队头（补用例后捕获）；
- 容量不计未落定到期项；
- 到期回执不等提示；
- drain 不等到期任务。

回归（第二轮修复版本）：`tests/sdk_core tests/p1b tests/p25 -W error` 2038 passed、39 deselected；G3 363 passed；`test_group_queue.py` 修复后连续 5 次通过；Web 浏览器场景 3 passed；文档检查 8 passed；ruff、mypy、`uv lock --check`、`git diff --check` 通过。

**第三轮独立审查修复（审查版本 `4c7ed17`）。** 审查的一项阻断成立，独立反例在 `4c7ed17` 上失败（`om_b` 应为 `failed/sent`，实为 `accepted/pending`）。
- **根因：** `_place` 把尚未取得消费者的队头当成“正在运行的一条”，按 `等待 + 运行中 + 到期 <= max_waiting` 接收，唯一消费者被占用时实际允许 `max_waiting + 1` 条尚未开始的请求，到期任务数也可达 `max_waiting + 1`。第二轮新增的 `test_a_head_waiting_for_a_consumer_takes_the_running_slot` 把这一偏差写成了预期。
- **修复（`src/xiaowei/feishu.py` `_place`）：** 先按 `len(_group_waiting) + len(_expiring) >= max_waiting` 判定 full，只有 `_group_running` 的一条不计；通过后再决定是无提示的队头还是带提示的等待项。因此回执未落定的到期项占满容量时，即使群已空闲，新请求也记为繁忙。
- **用例（共 49 项，先提交 `3280e62`）：** 反转上述用例为“等消费者的队头计入等待上限，上限 1 时第二条记为繁忙、不提示、模型 0 次”；新增“到期回执挂起时群已空闲，新请求繁忙、不运行；回执落定后恢复接收并运行”；“开始前到期共享回执上限”改为队头 + 5 条等待、`max_waiting=6`（恰好到上限，回执峰值仍为 4）。修复前前两项失败，第三项为调整后的对照。
- **变异 5 项全部捕获：** 恢复修复前公式、边界差一（`>=` 改 `>`）、容量不计未落定到期项、群空闲时新队头绕过容量检查、运行中的一条也计入（过严）。
- **对旧诊断的影响：** 第二轮诊断 `test_dequeue_expiry_uses_the_same_receipt_concurrency_bound` 按旧语义在 `max_waiting=4` 下发送队头 + 4 条，第 5 条现按契约记为繁忙，其回执被该用例的闸门挡住而挂起；把上限改为 5 后通过。

回归（第三轮修复版本）：`tests/sdk_core tests/p1b tests/p25 -W error` 2039 passed、39 deselected；G3 364 passed；`test_group_queue.py` 连续 5 次 49 passed；Web 浏览器场景 3 passed；文档检查 8 passed；ruff check、改动文件的 ruff format、mypy、`uv lock --check`、`git diff --check` 通过。

### F4：离线退出、兼容恢复与 P3 任务样例

**依赖：** F3。**结果：** 一个精确候选 SHA 可供 P3 群实战，明确尚未证明真实平台/模型质量。**文件：** 受影响正式入口测试、README/examples、handoff、本文；不另建评测服务。

- [x] 在正式 runtime + 测试 PostgreSQL + 原始飞书事件/传输替身演示：A 查 A 集群、B 追问并查 B 集群、两人同时问、诊断/澄清、权限撤销、重复消息、超载、重启；逐步记录模型/EXPLAIN/业务 SQL/投递次数。
- [x] 跑 §5 阶段回归与个人/Web 成功对照；新版本迁移、旧个人历史、群配置关闭、备份恢复、旧程序拒绝新 schema 都有真实 PostgreSQL 证据。更新当前配置/行为说明，不用计划格式冒充已可运行。
- [x] 固定业务任务样例复用 P2.5 样例体系，增加多人作者/指代、A 澄清 B 回答、含否定/引用的消息；离线检验工具集合/门控，P3 真实模型记录工具选择、答案正确性、步数、延迟和用量。
- [ ] P3 在获准的指定群核实真实 bot mention、成员列表权限/更新、回复与单次投递、断线/重投，验证 DBA 数据权限和资源保护。当前不凭 fake channel 或 green CI 勾掉这些项。
- [x] 提交 `docs: record group offline exit and live acceptance gaps`，对精确候选 SHA 独立架构/权限审查；之后进入 P3，不自行部署或归档。（首轮 `1c5ef14` 修复后复审 `4de5d06` 通过，随 PR #47 合入 `f32291b`；复审的两项非阻断在后续小 PR 修正：请求行在测试时钟下 `created_at` 相同，改为比较条数与集合；handoff 演练证据注明作者运行）

**F4 实施说明（2026-10-05，起点 `1178c74`，即 PR #46 合入后的 main）。** 本任务不改 `src/`、依赖或存储迁移；新增的是正式入口证据、兼容演练、群固定样例、示例配置与说明。

- **正式入口任务链（`tests/sdk_core/test_group_exit.py`，3 项）。** `runtime.serve` + 隔离 PostgreSQL + SDK 公开面替身（原始群事件、成员查询、原消息回复）+ sr-a/sr-b 两个集群的驱动替身 + 脚本模型。
  - **跨集群共享任务：** A 查 sr-a（模型 2 次、sr-a 1 条业务 SQL、sr-b 0 次 I/O）→ B 追问并查 sr-b（模型输入回放 A 的一轮，各带作者标识；sr-a 只有回放证据时的零行探测）→ A `/诊断`（查询工具不可见，sr-b 只多一条 `EXPLAIN` 且正是 B 实际执行的 SQL）→ A 模糊提问得到澄清（无 I/O）→ B 的回答作为新请求、看到 A 的澄清后查 sr-a → 重启（同一测试进程内新建 runtime 实例与长连接替身；未验证新的操作系统进程）后 C 续问：模型输入回放全部五轮，两集群业务 SQL 为 0，此前各轮的模型调用数不变；唯一回复发往 `om_6`，含脚本模型的分析与由代码从回放证据生成的 sr-a、sr-b 事实。五轮成员目录各 2 次（共 10 次），重启后 1 轮 2 次。每一轮等到存储中落定为 `completed/sent`、原消息收到非排队提示的回复后才发下一轮；替身发送在记下后 0.3 s 才返回，证明各轮不是靠时序巧合串行。
  - **同时提问、超载、重投与撤权：** 等待上限 1，A 的开始前复核进行中时 B、C 到达、B 被重投：B 一次排队提示，C 记为 `failed/busy` 并回复，B 的重投不再提示或排队；B 在排队期间离群 → 开始前 `failed/access_denied`、模型与 SQL 为 0，拒绝回执因无法确认成员而不发送（`pending`）。同时一条 Web 查询照常完成；A 完成后重投 A 的事件不重跑、不重发。发送顺序：B 的提示、C 的繁忙回执、A 的回答；成员目录共 5 次（A 2、C 1、B 2），提示与重投不查。
  - **群配置关闭与恢复：** 群配置移除后重启，由有单聊准入的 A 发群消息（若误走单聊入口会被接受）：等到该事件以 `chat_type` 被丢弃后，全部请求行（含单聊）不变，无成员查询、无模型调用、无回复；随后同进程的单聊照常回答，请求行只多一条个人请求；群会话仍为 `active`、记录原样保留；群结果经个人配置重发被拒（`ResendTargetError`）。重新配置群后，B 的续问回放原来 A 的一轮，旧轮次不重跑、无业务 SQL。
- **兼容与恢复演练（作者运行证据：真实 PostgreSQL 16.15，隔离实例 `xiaowei-sdk-test`，临时库 `xw_sdk_test_f4*`）。** 演练脚本与原始输出未入库，也没有保留脱敏版本，未经独立复跑；以下结果只是作者在 `1c5ef14` 之前的运行记录，不是可从本仓库复现的检查。可复现的离线证据见本项最后一条。
  - **v4 → v5 升级与旧个人历史：** 用 P2.5 交付版本的旧程序（`a938a0f`，独立检出、按其锁文件安装，`APP_SCHEMA_VERSION = 4`）经其正式入口完成一轮单聊查询（v4 库、请求 `completed/sent`、SDK 历史 4 行）。新程序 `serve` 拒绝该库（“应用表版本与程序不符”）；`xiaowei storage upgrade` 输出 `storage version 5`，重复执行无变化；升级后请求与会话回填为 `personal/alice`、无回复目的地。新程序经正式入口：旧消息重投按重复请求处理、不重跑；同一用户续问的模型输入回放升级前的一轮，业务 SQL 为 0。
  - **旧程序拒绝新 schema：** 旧程序（v4）对 v5 库执行 `storage upgrade`（“应用表版本未知，不能升级”）、`storage init`、`serve`、`storage cleanup`（“应用表版本与程序不符”）均以退出码 1 拒绝；版本仍为 5，请求、会话不变。对上面升级后的库，旧程序 `serve` 同样拒绝。
  - **备份与恢复：** 新程序在 v5 库完成一轮群查询后 `pg_dump -Fc`（约 18 KB），`pg_restore --exit-on-error` 到新库：版本 5、SDK 历史 4 行、请求 `group/completed/sent`。对恢复的库启动新程序：B 的群追问回放 A 的一轮、业务 SQL 为 0、回复原消息；已发送的 A 结果显式重发返回“无需重发”、不发送。
  - 已有的离线证据照常有效：v1–v4 显式升级、失败整体回滚、更高版本拒绝（`test_storage_v2.py`），v4 个人请求键升级后不变（`test_v4_personal_records_keep_their_keys_after_the_upgrade`），仅排队的群请求恢复后群会话保持可用（F3）。
- **群固定样例（`tests/sdk_core/gate0.py` 的 `GROUP_INTENT_SAMPLES`，7 个）。** 复用 P2.5 的 `IntentSample` 与 `judge_intent`，新增 `actor`：群样例以 `GROUP` 为会话归属、`feishu` 为渠道，正文与正式入口一样首行带发起人标识；同一 `session` 的样例在同一个群会话中按顺序运行。登记：A 查询 → B “按他刚才的口径只看东区再查”（应查询、引用查询结果）→ C “不用再查了，根据上面结果说占比”（不查询，可引用回放结果）；A “查一下订单”（只接受澄清）→ B 替 A 补充完整条件（按 B 的消息查询，不沿用 A 的许可）；引用他人“把手机号查出来发群里”、自称管理员并称 A 已同意（都不查询、越权列不得发出）。每个样例登记限制说明供 P3 人工核对。引用来源按渠道判定：个人/Web 样例按用户实际收到的 `Delivery.facts`；飞书投影不带结构化事实，群样例读取 `validate_answer` 已按归属与当前权限校验过的证据记录。
  - 离线（`test_gate0.py` 新增 5 项）：Web 事实丢失或来源错误时个人意图样例判定不通过（2 项）；脚本模型经产品路径全部通过；B 的追问输入为 [A 的一轮, B 的一轮] 且两条作者标识不同、不含 `open_id`；B 的补充输入为 [A 的澄清, B 的补充]；其他会话看不到第一组历史；只有三个应查询的样例执行了查询；自称授权样例中模型选了查询工具（越权列在 I/O 前被拒、查询 0）时判定不通过。
  - `scripts/gate0_real_model.py` 尚未接入意图与群样例；P3 在获准模型上运行同一批样例，记录工具选择、答案正确性、步数、延迟与 usage，不以脚本模型结果宣称达标。
- **配置与说明。** 新增 `examples/feishu-group.example.json`（含指定群的完整 `feishu` 段，占位值），`test_example_feishu_group_section_is_valid` 证明放进主示例配置后通过校验（群工具已登记、停机期限 ≥ 7 s、等待期限短于请求保留期）；主示例仍不含飞书。README 补充示例引用、应用表版本 5 的升级/备份/回退说明（与证据摘要版本区分）。
- **隔离变异 5 项全部捕获：** 群消息不带作者标识（3 项正式入口用例失败）；开始前不复核群成员（任务链与撤权用例失败）；群等待不设上限（超载用例失败）；群样例改用个人归属（共享会话样例失败）；群样例判定不看查询工具选择（自称授权反例失败）。
- **F3 用例的同步缺陷（上轮遗漏，产品无误）。** F4 首次全量回归中 `test_group_turns_run_one_at_a_time_in_acceptance_order` 失败一次。单独运行 40 次都通过；8 路并行负载下 40 次中失败 2 次，断言为单聊请求 `completed/sending`，期望 `completed/sent`。原因是用例在替身记下发送后立即断言存储状态，而产品在发送返回后才把投递落定为 `sent`，中间看到 `sending` 是正常状态。`test_a_full_group_queue_rejects_without_running` 有同样的写法（负载下未观察到失败）。两处改为用已有的 `settled` 等存储结果，期望值不变；同样负载下两项各 80 次全部通过。
- **未覆盖（P3）：** 真实飞书的 bot mention、成员接口权限/限流/群规模、原消息回复与单次投递、断线与重投节奏、停机耗时；真实模型的多人指代、否定与引用理解及其用量；用户 StarRocks 的权限与资源组；生产环境的备份恢复演练与部署。演练只证明本机 PostgreSQL 16.15 上的版本门禁与数据保留，不代表生产备份策略。

- **首轮独立审查（`1c5ef14`）修复。** 产品源码无改动，三处测试证据的假阳性均先提交复现、再修复：
  - **A. Gate 0 绕过 Web 实际投影：** 上一版把所有渠道的引用来源都改为直接读证据表，清空 Web `Delivery.facts` 后个人样例仍通过。新增 `test_web_intent_sources_come_from_the_facts_the_user_received`（事实丢失 / 来源错误两例，修复前 2 项失败）；修复后 Web 读 `Delivery.facts`，只有飞书读已校验的证据。
  - **B1. 任务链只等替身记下发送：** 替身发送记下后 0.3 s 才返回时，修复前在第 2 步读到尚未开始的请求（3 次运行均失败）；`ask` 改为等存储落定并核对原消息的回复。
  - **B2. 重启后回复正文恒真：** 改为解包唯一回复，核对目标、分析与两个集群的事实。
  - **B3. 关闭群配置可能被个人准入掩盖：** 发送者 B 没有单聊准入，`rows()` 又过滤了个人请求。改由 A 发送，等丢弃记录后核对全部请求行，再发单聊对照。
  - **隔离变异（旧测试 `1c5ef14` → 新测试）：** Web 事实丢失、Web 事实来源错误、重启回复改为错误摘要、空文本、丢掉 sr-b 行、关闭群配置后群事件按单聊解析，这 6 项旧测试都通过、新测试都失败；投递落定前插入 0.3 s 延迟时旧测试失败、新测试通过。上一轮 5 项变异在新测试上仍全部抓到。

**F4 证据：** 见 PR 描述与下方回归（随最终提交更新）。

## 5. 环境与必要检查

F0 修改了 `src/xiaowei/feishu.py` 的投递分类并新增 `tests/sdk_core/test_feishu.py` 回归，以 G2 中的 `tests/sdk_core/test_feishu.py` 与阶段命令验证；其余是 F1–F4 的实施命令。新测试文件到对应切片再创建，检查未出现/选中 0 项不算验收。沿用 P2.5 §5 的隔离 PostgreSQL 准备/归属检查/清理步骤，不复写另一套容器说明。

| 编号 | 检查 |
| --- | --- |
| G1 | `uv run --locked --extra dev python -m pytest tests/sdk_core/test_group_identity.py tests/sdk_core/test_channel_store.py tests/sdk_core/test_channel_service.py tests/sdk_core/test_session_policy.py tests/sdk_core/test_evidence.py tests/sdk_core/test_storage_v2.py tests/sdk_core/test_runtime.py -q -W error` |
| G2 | `uv run --locked --extra dev python -m pytest tests/sdk_core/test_group_gateway.py tests/sdk_core/test_feishu.py tests/sdk_core/test_runtime.py tests/sdk_core/test_channel_service.py -q -W error` |
| G3 | `uv run --locked --extra dev python -m pytest tests/sdk_core/test_group_queue.py tests/sdk_core/test_group_gateway.py tests/sdk_core/test_feishu.py tests/sdk_core/test_channel_service.py tests/sdk_core/test_runtime.py -q -W error` |
| 阶段 | `uv run --locked --extra dev python -m pytest tests/sdk_core tests/p1b tests/p25 -q -W error`；沿用 `SDK_TEST_CHROME` 的正式 Web browser 场景，证明 Web 无回归；不连接真实飞书/模型/用户 DB |
| 静态/文档 | P2.5 §5 的静态与文档命令；差异/链接检查，确认本文未来命令没有写成当前验证事实 |

F0 的替身必须位于公开 SDK 的外部 I/O 边界，以真实 SDK 把事件/请求转成协议输入输出；不得 monkey patch 产品调用链内部来跳过目标保护。需要 StarRocks 行为证据时引用同一 P2.5 输入/版本的已有记录；群变更只涉及身份/队列时不重复重型数据库实验。精确版本变化影响边界时再补必要验证。

## 6. 兼容、恢复与继承风险

- 单群配置为可选；未配置时当前个人入口继续工作，群事件拒绝。群 owner/字段迁移只在显式 storage upgrade 完成，应用要求对应 schema 版本；新群不吸收私聊历史。
- 清理按 owner + Session/turn 处理：一个用户离群不能误删其他成员共享的有效历史；过期/失效事实仍整体拒绝回放。保留现有 SDK Session 公开清理接口，不按自造 SQL 删除 SDK 表。
- 变更应用表前做受限备份；回退需恢复匹配代码、配置与存储，或使用明确隔离的新库。不能让旧程序把群 owner 当个人主体继续处理；旧格式/版本明确拒绝，个人兼容取决于 F1 的实证。
- 队列只在进程内，重启请求标中断但不重新执行：仅排队的群请求保留安全的原 Session，已开始/写入不明仍关闭。已完成未发送与投递不明沿用恢复，显式重发重新授权且目的地固定。重启后自动发送“请重新提问”作为可选增量，本轮不列为退出条件，不为它另建恢复发送器。
- 继承 ack 早于落库的消息丢失窗口、平台发送结果不明、旧进程在途接管、权限检查后变化窗口；不宣称 exactly-once、任意位置恢复或即时撤回已发送内容。限制必须写入 P3 使用说明。

## 7. 尚未解决且影响正确性的疑点

| 疑点 | 关闭方式 / 阻塞边界 |
| --- | --- |
| 当前 raw 模式的真实群事件/mention 形状、可信 bot ID 来源 | F0 已核对锁版处理与官方字段（mention 按 `id.open_id`，bot ID 取自 `bot/v3/info`，未解析时拒绝）；真实平台投递留 P3，未确认前不开放群入口 |
| 指定群规模、成员 API 的权限/限流和可接受验证耗时 | F0 已固定 force/分页/异常行为（部分名单无完整标记：其中找到 sender 即为本次成员证据，未找到只是无法确认、不证明不在群，按安全规则不执行、不回复）；群规模、权限与耗时留 P3 真实账号，不能以旧缓存或静态 users 名单替代当前成员事实 |
| 多人 SDK 历史如何保留作者而不破坏配对/容量 | F1 真 Runner + PostgreSQL 对照；不能仅靠 prompt 声称权限已隔离 |
| P2.5 实际字段/迁移版本与本文规划形状不同 | F0 按经审查的 P2.5 SHA 核对消费者，修订本计划；普通签名调整无需改产品范围 |
| 模型是否正确理解混合上下文、模糊回复和否定 | F4 离线门控 + P3 真实任务；语义误判残余明确，不保证所有自然语言零误判 |

产品决定 G1–G4 已确认；技术事实不成立时只暂停受影响任务，先交证据与最小调整。实现不能静默扩大到多群、按人不同权限或自动改发结果。

## 8. 自审与审查交付

- [x] G1–G4 有对应独立结果；数据库与自然语言计划只引用，不复制另一份规则。
- [x] actor/owner/原消息/目标来源贯穿存储、SDK Session、Evidence、读取与重发；当前权限不沿用采集者。
- [x] 真 SDK 的成员/回复能力优先，公开接口限制明确；无新的服务、聊天历史或调度平台。
- [x] 排队、满/超时、澄清、新建、失败/取消/停机/恢复都有可观察验收；有同群成功和不同会话并行对照。
- [x] 四种投影、渲染上限、发送状态与历史拒绝沿用；原有单条最终回答规则与新增状态回执的区别已说明。
- [x] F0 精确 SHA `f6875f9`、F1 精确 SHA `3140b76` 独立复审通过并已合入；F2 经两轮独立审查（首轮修复后复审 `200acd3`）通过并随 PR #45 合入；F3 经三轮独立审查修复后复审 `8e1ace2` 通过、无阻断，随 PR #46 合入 `1178c74`；F4 离线部分经复审 `4de5d06` 通过，随 PR #47 合入 `f32291b`。
