# 飞书单群：共享会话、有界排队与当前发起人授权实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. 按依赖串行交付，每片提供提交 SHA 和独立审查；计划通过不等于实施、合并或部署授权。

**Goal:** 指定群成员 @小维 后能用自然语言查询、诊断并共同追问；两人同时提问按群内接受顺序处理，权限、事实来源和回复归属始终清楚。

**Architecture:** 扩展现有 FeishuGateway → ChannelService → Application/PolicySession → GovernedTools → Evidence/ResultDelivery。SDK 驱动 Agent；当前发起人和群会话归属分离，现有进程内有界队列支持一个群的 FIFO，不增加 Worker、Redis、第二套历史或多群管理。

**Tech Stack:** 沿用 P2.5 交付锁文件；本次规划核对的版本为 Python 3.11.16、openai-agents 0.22.3、lark-channel-sdk 1.4.0、SQLAlchemy 2.0.52、PostgreSQL 16。

**Spec:** 产品唯一权威为 [ARCHITECTURE §7 G1–G4](../../../ARCHITECTURE.md#group-scope)，自然语言用途引用 [§5](../../../ARCHITECTURE.md#turn-purpose)，当前数据权限引用 §9。数据库能力、执行前评估、用途识别和数据权限验证由 [P2.5 计划](2026-10-03-p25-open-read-multi-cluster.md) 交付，此处只定义群边界。

**Baseline / 状态：** 文档候选 v1（2026-10-03）；规划时源码为 `7a715ff61d7a97457b03bb8b596ef4238c1049f2`（与本地 origin/main `0f831ebe070e7b11b1597fbdf59921b19981b129` 树相同）。实施起点必须换成 P2.5 Task 8 经审查的精确 SHA，复核其真实接口后再执行 F0。F0–F4 均未实施，当前无真实群验证证据。

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
| `app.py` / `governance.py` | P2.5 用途识别、SDK Loop、业务前治理 | 消费本轮可信群范围，不另建群专用 Agent 或工具集合 |

当前 `_parse` 只接受 p2p 且 sender 必须映射配置 users；ChannelStore 的会话和请求键含 subject_id；Evidence/Session 也以个人身份绑定。只开放 chat_type 会造成每人独立历史或越权，须检查全链。当前消费者池从全局队列取任务，Application 遇同 Session 繁忙会拒绝；它不等于本计划的同群等待。当前发送只带 chat_id，`runtime.resend` 由操作者重填 chat_id；群结果须补齐原消息目的地绑定。

### 1.2 调用链

```text
SDK 长连接 raw 事件（沿用先 ack 的已接受限制）
  → app / tenant / chat / user / text / 时间 / 本 bot mention 校验
  → 当前成员与共享策略检查
  → 持久接受：去重、actor、群 owner、原消息、当前 Session generation
  → 群 FIFO（有限长度/等待期限；必要时固定状态回执）
  → 出队：重验当前成员/授权/会话 → 获取一轮运行槽
  → P2.5 的历史复核 / SDK 用途识别 / 业务 Agent / 受治理工具
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

## 2. 跨边界契约

### 2.1 单群配置、入站和成员权限

- 可选配置只允许一组 app/tenant/chat 与明确的共享查询策略，空配置仍是当前单聊。bot 的 open_id 来源须与 app 匹配、由可信配置或受测的 SDK 身份接口取得；不得用显示名称识别本机器人。
- 入口逐项检查消息类型、sender_type=user、app/tenant/chat、平台 mentions 中本 bot ID、时间与正文大小。只删除与平台 mention 对应的 token，再处理命令/自然语言。纯文字 `@小维`、@其他 bot、缺失/伪造 mention、其他租户/群、机器人和超龄事件均不进入 Agent、模型或数据库；不向非指定群回复。
- 保留 tenant_id（映射平台 tenant_key）、chat_id、sender_id（open_id）、message_id，并连同 app 与入口种类形成可信身份。群用户不需要出现在单聊 users 中，群授权不得使该用户自动获得单聊/Web 访问。
- AccessPolicy 接受可信群上下文：当前配置仍允许该群、当前 actor 仍为成员才返回同一组获准目标/工具。每轮开始及工具执行前/结果交付按现有治理边界复核；成员 API 走公开 SDK、force=True、固定页数与期限，只在一次边界操作内复用证据。未找到、超时、限流、响应不合约均 fail closed，无旧名单宽限期。
- 成员目录只给治理使用，不把全群名单交给模型/Session；不配置可替换成员真实性的 resolver hook。页数/期限在配置中有有限上界，F0/P3 确认指定群规模能覆盖，超出上界提示能力不可用，不悄悄把新成员当白名单拒绝。
- 成员检查与发送不是同一平台事务，不能保证刚查完离群时原子撤权；下一边界重验，已发消息不自动撤回。配置移除群后停止新接受，旧排队请求执行前拒绝。

### 2.2 actor、owner、Session 与 Evidence

- actor 是当前 sender 的可信身份；owner 是应用/渠道/租户/群所决定的共享会话归属。两者是不同字段/含义，不用“假用户 ID”同时充当两者。私聊与 Web 沿用个人归属，不因群改动放宽。
- 群 Session 由 owner + generation 及已有 ModelBinding 约束生成；同群不同 sender 使用同一当前 Session。`/新建` 在空闲时轮换整个群，保留期与历史上限沿用现有契约；运行中或有排队请求时拒绝，避免接受时与执行时绑定不同代。
- 每条请求/Evidence 保留原 actor、turn 与目标来源；共享可读性按当前 owner、当前请求者资格和 P2.5 数据权限复核，不能通过抹去 actor 或取消 ownership 检查实现共享。A 收集的事实在同群可由 B 追问，其他群/私聊/Web 不可读，即使是同一个人。
- 历史需要区分当前发起人与各轮作者，利用既有请求记录与 SDK 公开输入格式给出可信、有限的作者/轮次标识；不得依据正文“我是管理员”推断身份，不建另一张聊天历史表。保留 SDK tool call/result 配对，角色/标识的投影也计入容量。模型能理解标识不等于取得额外权限。
- B 回答 A 的澄清并不自动继承 A 的查询许可；本轮从 B 的消息及可读群上下文重新判断。信息指代不清时澄清，不能任取最近一人的意图。数据库工具与自然语言分类契约直接引用 P2.5，不在群层另写一套。
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
- 队列满立即保存受控失败/固定回执；到期项在有界定时检查或出队时结束，最迟在配置的检查间隔内提示，不等前一条无限结束。满/到期/成员失效均不调用模型或数据库，不重新入队。队头失败或普通取消仍能释放 active，使下一条继续；存储/实例所有权故障则停止渠道，不能带着不明状态继续。
- 等待人类澄清不占队头：提交澄清回答后结束该轮，其他成员可继续；下一条回答仍需 @ 并按新请求接受。不开 SDK interruption 长时间挂起，也不实现会话续跑框架。
- Web/单聊保持原来的同会话繁忙提示；不同会话可以在全局上限内并行。群内跨两个集群仍属一个共享 Session，必须串行。
- drain 先关入口，等待已进入 receive 尚未落库的请求以及所有群/普通队列，在同一绝对期限内处理或取消。重启不恢复 FIFO 执行，只按现有恢复把 accepted/running 标中断、未知投递标 unknown；用户明确新发才是新任务。成员检查任务与传输线程都必须关闭，禁止退出后继续发送。

## 3. 失败处理总表

| 场景 | 对用户的行为 | 需要证明的边界 |
| --- | --- | --- |
| 无 @ / 非指定群 / bot / 伪造身份 | 不回复 | 无 Agent、模型、业务 DB；不触发无关成员查询 |
| 当前成员或共享策略无法确认 | 指定群内仅安全拒绝提示 | 无旧事实/名单泄露；无 Agent、业务 DB |
| 同群已有一轮 | 有界排队，必要时固定提示 | 新任务未提前读取历史/调用模型；其他会话有槽可运行 |
| 队列满/等待到期 | 明确未运行，可稍后重新发问 | 记录失败；模型与 DB 调用为 0；重投不刷新期限 |
| 排队中成员离群或群配置撤销 | 未获准执行 | 出队重验失败，无历史回放/模型/DB |
| 当前 SQL 用途不明 | 正常澄清并结束此轮 | 查询工具不可执行，释放群槽位 |
| PostgreSQL/实例锁/入队交接失败 | 服务暂不可用 | readiness 关闭，不遗留假“处理中”并继续接活 |
| 原消息删除或发送失败/未知 | 保存真实投递状态 | 不换目的地、不重查、不自动重发 |
| 当前数据权限或 Evidence 失效 | 不交付旧事实，必要时新建会话 | 与 P2.5 同一验证器，群共享不能绕过 |
| 重启/停机超时 | 请求中断或投递未知 | 不自动恢复模型/SQL；不声称远端已停止 |

## 4. 按独立结果拆分的任务

依赖均为独立审查通过的精确 SHA。先检查 P2.5 实际公开接口，不根据本文旧函数形状批量实现。

### F0：锁定 SDK 群协议与最小接入方式

**依赖：** P2.5 Task 8 与本计划经审查。**结果：** §1.3 的能力在离线协议边界可复现，指出真实平台待验证项。**文件：** 本文/handoff；临时探针在工作树外，不改产品代码。

- [ ] 用锁定 SDK + 受控传输替身核实 raw 群消息在当前 policy 配置下的到达顺序、mentions 的 ID/key 与文本 token、sender/tenant 字段、bot 身份取得方式。正式事件类型与边界按锁定 SDK/官方契约核对，不能只调小维 parse helper。
- [ ] 公共 get_chat_members 覆盖 force 缓存绕过、分页、部分列表中找到/未找到成员、权限失败、限流、超时；证明没有复用陈旧成功名单或访问私有辅助函数。
- [ ] 公共发送路径验证 reply_to、reply_target_gone=fail、单次 retry、单条文本；原消息消失时不得触发 create-message fallback；结果失败/未知分类不靠捕获所有异常说成功。
- [ ] 记录 P3 所需平台事件订阅、机器人入群和成员/回复 API 权限；没有真实平台不能宣称已可入群。若 public API 无法满足契约，先提出具体最小调整，不另写一套鉴权/传输 SDK。
- [ ] 提交 `docs: record Feishu group SDK contract evidence`；以版本和安全输入/输出独立审查后进入 F1。

### F1：可信群身份、共享归属与存储闭环

**依赖：** F0。**结果：** 同群 A/B 共享会话和获准事实，所有入口仍按当前 actor 与 owner 复核。**文件：** models/channel/channel_store/session/evidence/runtime、下一应用迁移；对应现有测试及 `tests/p25/test_group_identity.py`。

- [ ] 先在测试 PostgreSQL 保存 A 的请求/查询 Evidence，让 B 在同群回放/验证/读取；验证新查询保留 B 为 actor、旧事实保留 A 为采集者。无变化成功对照必须经过真 PolicySession/SQLAlchemySession。
- [ ] 同一 actor 的私聊、Web、其他群/租户、不同 generation/ModelBinding 全部不能读群事实。伪造 owner、receipt、request_ref、同 ID 改 sender/chat/正文、旧个人记录混入群都拒绝；拒绝时模型/业务 SQL 为 0。
- [ ] 检查 ownership/fingerprint 的全部调用者，包括 RequestStore 条件更新、Evidence record/read、最终提交、读取、resend、恢复/清理。添加最少的应用迁移与回复目的地字段，SDK 表不改。
- [ ] 当前成员成功/失败/未知，排队后撤权与最终交付前撤权均覆盖；新配置不能把群权限赋予私聊。个人授权/历史/重发成功对照保留。
- [ ] 运行 §5 G1；隔离变异去掉群隔离、沿用原采集者授权、抹去当前 actor、替换保存目的地分别应失败。提交 `feat: separate group conversation ownership from turn actors`，独立审查。

### F2：指定群 @ 入口与原消息交付

**依赖：** F1。**结果：** 一个指定群可经正式链路进入 P2.5 Agent，严格按原消息交付，Web/单聊语义保留。**文件：** feishu/config/runtime/channel；`test_feishu`、`test_runtime`、`test_channel_service`、新增 `tests/p25/test_group_gateway.py`。

- [ ] raw 事件 → LarkTransport → Gateway → ChannelService → 真 Runner/测试存储 → ResultDelivery → SDK 协议替身的成功链，不能只测试构造 InboundRequest。
- [ ] 未 @、@其他 bot、仅正文伪造 @、非指定群/tenant、机器人、附件/超长/超龄事件均无 Agent/模型/DB；保留四项身份，body 不能覆盖它们。群成员未配单聊 users 仍可在指定群使用。
- [ ] 沿用 P2.5 自然语言及快捷命令；诊断/生成/模糊轮不执行 SQL，当前 actor 与有界作者标识进入正确的历史/模型位置。`/新建` 的群语义有提示。
- [ ] 同 message_id 重投、并发重投、发送失败/未知/原消息删除、目的地篡改与显式重发均验证；排队通知不能占最终投递权，事实/分析/来源截断保持 P2 规则。
- [ ] 运行 §5 G2；变异跳过 mention/当前身份/原消息绑定、允许 SDK fallback 或重复执行应失败。提交 `feat: admit mentioned group messages and bind replies to their origin`，独立审查。

### F3：同群有界串行与不同会话并发

**依赖：** F2。**结果：** 同时 @ 有可观察的先后和限额，队列不会变成无界积压或阻塞全部会话。**文件：** feishu/channel/config/runtime；现有生命周期用例及 `tests/p25/test_group_queue.py`。

- [ ] 用事件屏障控制 A/B/C 接受/开始/结束，断言按接受顺序、同群最大 active=1；B 开始时可见 A 已提交的历史。另一个单聊/Web 同时能运行，总 active 不超过原全局上限。
- [ ] 队列长度等于/超过上限、等待刚好/超过期限、前一轮超时/异常、通知失败、重复事件、排队中 actor 离群、存储失败和实例锁丢失均有成功/拒绝对照；到期失败无需等待整个长任务结束。
- [ ] 澄清释放队头、后续成员可问新问题；`/新建` 在运行/排队时拒绝、空闲可轮换。群跨目标也不并发读写一个 Session。
- [ ] drain 包含落库前 receive、群 FIFO、普通队列和在途发送；停机期限、取消、重启恢复不重放 SQL。失败/取消能释放槽，存储不明不能继续接活。
- [ ] 运行 §5 G3；隔离变异去掉同群串行、容量/期限、出队重验或让每个等待者占 worker 槽应失败。提交 `feat: queue group turns within bounded channel concurrency`，独立审查。

### F4：离线退出、兼容恢复与 P3 任务样例

**依赖：** F3。**结果：** 一个精确候选 SHA 可供 P3 群实战，明确尚未证明真实平台/模型质量。**文件：** 受影响正式入口测试、README/examples、handoff、本文；不另建评测服务。

- [ ] 在正式 runtime + 测试 PostgreSQL + 原始飞书事件/传输替身演示：A 查 A 集群、B 追问并查 B 集群、两人同时问、诊断/澄清、权限撤销、重复消息、超载、重启；逐步记录模型/EXPLAIN/业务 SQL/投递次数。
- [ ] 跑 §5 阶段回归与个人/Web 成功对照；新版本迁移、旧个人历史、群配置关闭、备份恢复、旧程序拒绝新 schema 都有真实 PostgreSQL 证据。更新当前配置/行为说明，不用计划格式冒充已可运行。
- [ ] 固定业务任务样例复用 P2.5 样例体系，增加多人作者/指代、A 澄清 B 回答、含否定/引用的消息；离线检验工具集合/门控，P3 真实模型记录工具选择、答案正确性、步数、延迟和用量。
- [ ] P3 在获准的指定群核实真实 bot mention、成员列表权限/更新、回复与单次投递、断线/重投，验证 DBA 数据权限和资源保护。当前不凭 fake channel 或 green CI 勾掉这些项。
- [ ] 提交 `docs: record group offline exit and live acceptance gaps`，对精确候选 SHA 独立架构/权限审查；之后进入 P3，不自行部署或归档。

## 5. 环境与必要检查

本轮仅写文档；下列是实施命令。新测试文件到对应切片再创建，检查未出现/选中 0 项不算验收。沿用 P2.5 §5 的隔离 PostgreSQL 准备/归属检查/清理步骤，不复写另一套容器说明。

| 编号 | 检查 |
| --- | --- |
| G1 | `uv run --locked --extra dev python -m pytest tests/p25/test_group_identity.py tests/sdk_core/test_channel_store.py tests/sdk_core/test_channel_service.py tests/sdk_core/test_session_policy.py tests/sdk_core/test_evidence.py tests/sdk_core/test_storage_v2.py tests/sdk_core/test_runtime.py -q -W error` |
| G2 | `uv run --locked --extra dev python -m pytest tests/p25/test_group_gateway.py tests/sdk_core/test_feishu.py tests/sdk_core/test_runtime.py tests/sdk_core/test_channel_service.py -q -W error` |
| G3 | `uv run --locked --extra dev python -m pytest tests/p25/test_group_queue.py tests/sdk_core/test_feishu.py tests/sdk_core/test_channel_service.py tests/sdk_core/test_runtime.py -q -W error` |
| 阶段 | `uv run --locked --extra dev python -m pytest tests/sdk_core tests/p1b tests/p25 -q -W error`；沿用 `SDK_TEST_CHROME` 的正式 Web browser 场景，证明 Web 无回归；不连接真实飞书/模型/用户 DB |
| 静态/文档 | P2.5 §5 的静态与文档命令；差异/链接检查，确认本文未来命令没有写成当前验证事实 |

F0 的替身必须位于公开 SDK 的外部 I/O 边界，以真实 SDK 把事件/请求转成协议输入输出；不得 monkey patch 产品调用链内部来跳过目标保护。需要 StarRocks 行为证据时引用同一 P2.5 输入/版本的已有记录；群变更只涉及身份/队列时不重复重型数据库实验。精确版本变化影响边界时再补必要验证。

## 6. 兼容、恢复与继承风险

- 单群配置为可选；未配置时当前个人入口继续工作，群事件拒绝。群 owner/字段迁移只在显式 storage upgrade 完成，应用要求对应 schema 版本；新群不吸收私聊历史。
- 清理按 owner + Session/turn 处理：一个用户离群不能误删其他成员共享的有效历史；过期/失效事实仍整体拒绝回放。保留现有 SDK Session 公开清理接口，不按自造 SQL 删除 SDK 表。
- 变更应用表前做受限备份；回退需恢复匹配代码、配置与存储，或使用明确隔离的新库。不能让旧程序把群 owner 当个人主体继续处理；旧格式/版本明确拒绝，个人兼容取决于 F1 的实证。
- 队列只在进程内；重启统一标中断，不把持久 accepted 记录当可重新执行的任务列表。已完成未发送与结果不明沿用原状态恢复，显式重发需重新授权且目的地固定。
- 继承 ack 早于落库的消息丢失窗口、平台发送结果不明、旧进程在途接管、权限检查后变化窗口；不宣称 exactly-once、任意位置恢复或即时撤回已发送内容。限制必须写入 P3 使用说明。

## 7. 尚未解决且影响正确性的疑点

| 疑点 | 关闭方式 / 阻塞边界 |
| --- | --- |
| 当前 raw 模式的真实群事件/mention 形状、可信 bot ID 来源 | F0 官方/锁版协议核对 + P3 真实群；未确定不开放群入口 |
| 指定群规模、成员 API 的权限/限流和可接受验证耗时 | F0 固定分页/异常行为，P3 真实账号；不能以旧缓存或静态 users 名单替代当前成员事实 |
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
- [ ] 针对最终文档精确 SHA 独立审查；F0 和后续实施仍未开始。
