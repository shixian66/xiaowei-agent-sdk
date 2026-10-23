# 切片 A：定时 Agent 首跑故障与失败可见性

> 状态：已获用户批准离线实施，首跑与固定失败通知已实现，相关回归 42 passed，补充图片/日志/文档检查 15 passed，lint/type 通过；待独立审查。A → [B](2026-10-23-security-inspection-incomplete-results.md) → [C](2026-10-23-security-plain-notification.md) 各自分支、差异和审查；不调用真实模型或公司服务，不合并、打包或部署。

## 1. 目标、基线与交付

让获批定时 Agent 能在装配图片能力的环境正常首跑；失败时保留真正原因码，并在现有发送授权允许时给目标群一次固定未完成说明。

- 调查基线：公司回报 `main@1de7b79d61e0c934c5235a522dceb96ccd28aaf5`；本任务工作树 HEAD `593fa033579db16eab8850d79927b864c7cf8234`，文件树与该 main 相同。已有 C 计划及桌面原目录修改保留。
- 拟用分支：`codex/scheduled-agent-first-run-recovery`，实施时核对获准基线。用户本轮已授权连续实施，B 从本片独立审查通过的基线另建分支，C 同理；分别记录精确 base/head，不把上片差异混作本片成果，不自动合并。
- 本片不改审计检测、结构快照、业务调度或任务时间语义，不做例行操作白名单。首跑恢复与固定失败通知一起审查；B 的单集群部分失败走 B 的正常结果链，不另发 A 的第二条通知。

## 2. 根因与已取得证据

| 位置 | 已核实事实 |
| --- | --- |
| `runtime.py:1848–1850`；`app.py:747–756` | 图片存储随模型图片能力装配；`_turn` 在 storage_ready 后无条件为普通新轮次读图片，未排除可信 automatic 任务。 |
| `scheduled_tasks.py:1006–1044`；`image_input.py:158–207` | 定时运行使用原群身份、新的独立 session_id；图片读取只按 channel=feishu 进入，再要求 channel_session 记录存在，独立任务没有该用户图片会话记录。 |
| `app.py:239–275,316–334,1532–1555` | TurnReason 有 15 项，默认文案缺 session_unavailable、content_rejected、storage_failed。ImageStoreError 映射不传 message，构造 TurnError 时发生二次 KeyError。 |
| `app.py:675–695`；`scheduled_tasks.py:1121–1146` | `_turn_error` 本身抛错，使 stage=failed 尚未记录；外层只记录异常类型并按 execution_failed 落定，掩盖原始分类。 |

未修改源码的临时探针 `/tmp/test_xw_security_plan_probe_a.py`，日志 `/tmp/xw-security-plan-a-repro.log`，结果 **4 failed、13 passed（2.32s）**，预期失败为三项缺文案及图片存储场景：

- 无图片存储对照：completed / execution_completed，模型请求 2，POST 1。
- 接入真实 SessionImageStore：failed / execution_failed，模型请求 0，POST 0，日志 error=KeyError。
- 探针沿正式群任务起草、批准、tick、Application、真 SDK/治理与隔离 PostgreSQL；模型、StarRocks、飞书和 HTTP 为合成替身。为隔离 provider 协议，图片存储通过真实 Application 的依赖装配点接入，**没有验证真实 Vertex 服务或公司配置**；实施回归需补“合法图片 Profile + Vertex 协议替身”的完整装配对照。

这已证明代码路径可达，不能据此认定公司当时的模型图片配置；公司根因最终对应仍需核对该布尔配置与请求日志，不索取凭据。

## 3. 推荐修复

### 3.1 默认文案与图片作用域

补齐三项固定提示；测试遍历 `typing.get_args(TurnReason)`，逐项构造默认 TurnError，断言 reason 保留且文案非空。不新增任意异常字符串兜底或 `.get(..., 成功)` 分支。

在 `Application._turn` 沿现有可信 `ctx.automatic` 排除图片加载：独立定时任务没有用户图片输入。普通飞书图片加载、归属、期限与原值复核不变；不把 ImageUnavailableError 全局吞成空图片，也不给定时会话伪造 channel_session 行。SDK Runner、PolicySession 与群身份不改。

### 3.2 可定位且不泄漏的失败日志

在 `_run_agent` 的既有阶段边界维护局部 stage：prepare_tools / run_turn / validate_delivery / render / send / settle。失败日志输出 job_id、run_id/turn_id、固定 stage、TurnError.reason（其他异常用 execution_failed）和现有异常类型；可取得时附既有 tool_error 码。

复用 app 的安全 stage/usage 日志，不输出异常正文、堆栈、任务正文、SQL、数据、账号、URL 或凭据，不新建 trace/日志平台。未启动模型时仍应有失败阶段及 model_requests=0；通知失败单独记原因，不能覆盖任务的首个执行失败原因。

### 3.3 固定失败通知：建议做，但表述为“未完成”

用户已要求评估漏巡检提示。推荐：本次 run 已开始、尚无任何发送机会、实例仍就绪且当前任务/接收授权可复核时，复用 `WebhookNotifications.send_fixed`、`_claim_agent_send`、`_settle_agent_send` 发送一次。固定通知不调模型，不重跑业务，不新增发送队列。通知只在执行失败的 except 分支内、该分支的 `_finish_agent` 和 `end_turn` 之前尝试；发送或落定阶段失败不再进入失败通知。`_finish_agent` 自身失败锁住实例后不再尝试通知；此前尚未发送则总 POST 为零，已有发送则不追加。

示例：

> ⚪ 本次定时巡检未完成。任务〈ID〉；任务获准目标：fat1；计划时刻：2026-10-23 18:10:00+08:00；实际查询时间窗：未取得；原因：session_unavailable（会话暂不可用）。请核对后单独补查。

- 统一用“未完成”，因为模型/存储失败也可能发生在部分 SQL 已执行之后；不能一概声称“未执行”。B 会进一步按每次实际审计调用区分未执行/失败/未知。
- “任务获准目标”来自 AgentSpec.targets，只表示权限上界，不是巡检清单；计划时刻来自 scheduled_at。**AgentSpec 当前没有结构化审计窗口**，首个模型请求前失败时不能从自然语言任务正文可靠推导窗口，也不能把计划时刻当查询窗口。已取得的绝对窗口可在 B 接入后复用；未取得就如实标注。本片不增加时间解析器或任务模板 schema。
- 仅发送固定状态和可信元数据，不转发异常/任务正文。发生错误后须复用当前定义/主体/机器人目标及接收范围复核；这是新的失败发送路径，不能仅凭开始时的许可发送。
- 复用每 run 唯一通知记录：若已存在 sending/accepted/failed/unknown 记录，或错误发生在发送/落定阶段，不另发失败消息。保持原回执和未知结果门槛，不重试、不拆分、不转投其他群。
- 当前撤权、成员未核实、任务已停用/到期、存储不就绪或取消中断时，沿现有规则零 POST；不为发失败通知恢复执行或绕过状态。未成功发送仍可通过原日志/任务状态定位。
- 失败通知即使被接口接受，run 仍 failed/unknown，不改成 completed；代理 2xx 仍只代表 HTTP 接受。已有通知预算预留和容量上限继续适用。
- 连续多个 occurrence 失败时，各自在当前授权和发送条件允许下通知一次，不跨次去重或静默，让群知道哪一次未完成；实施时在 ARCHITECTURE 的既有定时交付契约中明确。
- 只使用既有预留的通知调用和单次 HTTP 总期限（10 秒），不重开 Agent 的整轮预算。执行阶段原上限不增加；若执行因自身超时结束，固定通知收尾最坏再占这一次有界发送期限，实施时在架构契约和测试中明确，不暗中恢复业务执行。

不采用“给所有飞书身份缺失会话放行”：会破坏真实用户图片边界。也不新增独立通知服务；现有固定发送链已提供所需账本和治理。

## 4. 新增限制与依据

| 检查或作用域调整 | 具体可达场景、现有机制为何不足 | 最小落点 |
| --- | --- | --- |
| automatic 不读取用户图片 | 新 session 是合法定时身份，却不属于用户图片会话；现有图片归属检查不能区分该用途。 | 复用可信 automatic 标记，仅跳过该 I/O，不放宽普通图片检查。 |
| 固定失败通知复核当前授权和任务状态 | 执行失败后可能已撤权、取消或失去存储就绪；该新路径没有成功回答的 Evidence 交付复核可依赖。 | 复用当前定义/发送治理、锁内 claim，不新增角色或配置校验。 |
| 已有发送机会时不追加失败消息 | 第一次 POST 可能已到达，落定失败不能再发第二条制造重复或覆盖 unknown。 | 复用同一 run 的唯一通知账本，不新增去重框架。 |

其他为现有 [架构边界](../../../ARCHITECTURE.md#scheduled-agent-tasks)，不把本期范围转成能力禁令。

## 5. 实施与验证

| 改动文件 | 职责 |
| --- | --- |
| `src/xiaowei/app.py` | 文案表补齐、automatic 图片作用域；SDK Loop 与 Session 不改。 |
| `src/xiaowei/scheduled_tasks.py` | 安全阶段日志、固定失败通知接线与原失败/发送原因分别保留。 |
| `tests/sdk_core/test_app.py`、`test_scheduled_agent.py` | 固化上述 RED；合法图片 Profile 的真实装配、正常与失败任务、发送恰好一次。 |
| `tests/sdk_core/test_image_store.py`、`test_feishu_group_images.py` | 选取既有普通图片归属/过期/跨用户拒绝回归；不新增相同用例或把两个文件全量设为门槛。 |
| `ARCHITECTURE.md`、`AGENT_HANDOFF.md`、本文 | 实施时更新失败通知契约、实际检查和剩余公司缺口；其他文档只需受影响时引用。 |

验证去重约定（B/C 引用本段）：每片按实际增量选择测试节点并记录命令、结果与版本；同一闭环或权限边界已覆盖且实现未再受影响时，复用既有用例与证据，不在后续切片另造相同场景。确定性展示组合用轻量单元测试，正式 SDK/存储/渠道只跑代表性闭环；不要求每个展示分支都重建一轮 Agent。真实模型/公司验收缺口及各片独立差异审查仍保留，完整默认回归由后续获准 PR 的最终 HEAD CI 执行，不新增测试选择框架。

串行完成：

- [x] 先固化缺文案与首跑失败用例，确认命中预期而非脚本耗尽或权限夹具错误。
- [x] 补文案与局部图片修复；正式群起草/批准后跑通一次真实 SDK 只读任务和一次可控失败，保留隔离 PG、治理及 HTTP 替身。
- [x] 补日志和失败通知；覆盖模型前失败、部分查询后失败、发送后落定异常不二发，以及当前权限撤销/unknown 门槛下零 POST。用合成敏感标记断言日志与固定消息均无泄漏。
- [x] 补尚未发送时 `_finish_agent` 失败并锁住实例的零 POST 用例，以及已发送后落定失败不追加通知；正常结果、周期下一次执行与其他任务继续工作。用受控时钟覆盖连续两次失败，各次恰好通知一次，不等待真实一小时、不跨次合并；同一 occurrence 不重跑，固定提醒 B 的既有行为不变。
- [ ] 跑相关用例、Ruff、改动文件 format、mypy、文档检查与 diff；独立审查本片精确 base/head。完整默认测试由后续获准 PR 的 CI 执行，不本地重跑全量。

命令沿 [README](../../../README.md)：锁定 `.venv` / `uv run --locked --extra dev python -m pytest -q` 按上述分支选择 `test_app.py` 的错误/日志、`test_scheduled_agent.py` 的首跑/失败通知及图片相关测试节点，不以整文件重跑代替范围选择；同一 `SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres`，只允许 loopback。执行 `uv run --locked --extra dev ruff check .`、`uv run --locked --extra dev mypy src`、`tests/security/test_docs_command_consistency.py`、`git diff --check`。不跑真实模型或发行测试。

## 6. 验收样例与兼容

| 用户操作 → 系统行为 → 用户结果 | 成功标准 / 不能接受 |
| --- | --- |
| A1：在启用图片能力的部署创建并批准文本巡检任务 → 到时使用独立 Session 与正式 Runner → 一条正常结果。 | 模型确实调用、工具受治理、POST 恰好一次；不能用关闭图片能力、伪造用户会话或 bypass 治理通过。 |
| A2：同一任务模型前或执行中故障，下一计划时刻仍故障 → 每次沿原账本发一次固定说明 → 群知道哪两次未完成。 | 不伪造检查窗口、不把部分执行说成未执行、不把通知接受说成巡检完成；不跨次静默或重复发送。 |
| A3：发送后回执未知、任务已取消/撤权，或 `_finish_agent` 失败锁住实例 → 沿原账本和权限处理 → 不追加 POST，状态/日志可查。 | 不自动补发、不绕过当前权限，原失败和发送原因不相互覆盖；落定失败不能触发第二条通知。 |

这些样例沿用到开发、审查和公司验收。离线证明调用链与固定内容，真实公司 Profile、渠道实收和当时配置仍未验证；建议公司验收只保留一次首跑正常、一次合成故障通知及脱敏请求/运行编号，不重做全部安全规则样例。

不改 Evidence、模型回答 schema、回答版本、应用 schema 或图片存储契约；不追补历史漏跑任务，不因新文案重发旧消息。若实现发现现有通知账本不足以承载该路径，先说明具体反例并局部修订，不能悄悄增加新发送机会。
