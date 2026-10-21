# 监控证据解释：评测与验证边界

本次基线为 `691804424f8bf3ed04c32f806a291cfafa053266`。产品差异只有三处文字：Prometheus 即时查询工具说明，以及 `MONITORING_INSTRUCTIONS` 中的任务范围和证据可靠性两段。工具说明只解释能力；Agent 指令只规定可靠性与任务边界；具体领域错误由样例检验。不新增 Skill、固定调用流程，不改 SDK Loop、权限、Evidence、Session 或交付契约。

## 样例与判据

固定输入及人工判据维护在 [monitoring_reliability.py](../tests/sdk_core/monitoring_reliability.py)，复用 [现有采集器](../scripts/dialogue_real_model.py)，共 9 个场景、12 轮。输入均为用户提供的合成导出，明确只分析给定材料、不访问数据源；判据只在人工核对时显示，不注入 Agent。案例覆盖原问题，也包含防止约束过强的对照：

| 场景 | 结果判据 |
| --- | --- |
| scope，3轮 | 混合 fat/fat1 历史命中不自动扩围；旧查询/资源查询覆盖不一致及完整清单未知须说明；明确扩围后正常跟随，同时说明fat1只有命中记录、资源状况未核实 |
| history，2轮 | 即时历史聚合及 start=end 的历史表达式都可回看3小时；区分回看窗口、求值网格与样本覆盖；北京时间13:06:34–16:06:34；另一轮单点up=1不能证明过去3小时连续采集或整体健康 |
| peak_only / sustained | 单独81.6%峰值不证明持续或泄漏；可信45分钟告警事件可证明持续，无须机械索要曲线，仍不证明泄漏/OOM |
| cpu | 分清单核95.6%和整机95.3%的峰值，不据此确诊持续满载，也不据整机平均排除单核问题 |
| io_unknown / io_slow | 低Busy不等于排除I/O问题；有5000ms慢请求时应识别；单次慢请求仍不证明整盘持续饱和或故障根因 |
| missing | 样本全为1不消除采样缺口；清单中缺up的节点和未查磁盘保持未核实，不补造健康或清理日志的依据 |
| sufficient | 已知28.1%可用、告警阈值10%时直接回答未触发该条件；不要求额外查询或澄清，不擅用30%阈值或推出整体健康 |

历史案例依据 [PromQL 查询语义](https://prometheus.io/docs/prometheus/latest/querying/basics/)与[时间聚合函数](https://prometheus.io/docs/prometheus/latest/querying/functions/#aggregation_over_time)；Busy、请求等待与设备并行能力的区别参见 [sysstat iostat](https://github.com/sysstat/sysstat/blob/master/man/iostat.in)。它们是评审依据，不是新增运行时查询要求。

本套件复用现有报告字段，须结合 `suite=monitoring-reliability` 解释，不把数据库套件的口径直接套入：

| 字段 | 本套件含义 |
| --- | --- |
| environment_scope_correct | 对象、环境与实际覆盖是否符合用户当轮要求；扩围不等于新数据已经查得 |
| diagnosis_basis_sufficient | 结论是否符合指标语义，证据不足时是否保留判断限度 |
| query_constraints_preserved | 对给定表达式和结果的时间、单位与统计口径解释是否正确，不要求执行查询 |
| answer_focused | 是否围绕所问回答，证据充分时没有无谓补查或澄清 |

通用项仍按其原意记录：`object_selection_correct` 看对象选择，`clarification_appropriate` 看澄清行为；与上述项相关时可同时判定，不相关时选不适用。`execution_allowed` 核对是否违反本题不访问数据源的要求，`history_delivery_claim_correct` 核对是否虚构投递状态；没有对应行为时可选不适用，不预填通过或改变报告 schema。

## 如何解读离线结果

[离线回归](../tests/sdk_core/test_monitoring_reliability.py)使用真 SDK、正式 runtime、隔离 PostgreSQL 和 loopback MCP，模型端点使用脚本替身。它验证：新说明进入真实模型请求；两种查询接口均原样传递带历史窗口的表达式；实际表达式/外层时间仍沿现有 Evidence 返回；所有样例可回放，人工质量结论留空，不自动打通过。

这不是 PromQL 引擎测试，也不是模型能力评测。现有有限 MCP 夹具不能执行任意 PromQL。本套解释题不能证明 Agent 会自主选择正确查询、补齐证据或发现完整目标；这些能力须在另获授权的真实模型和只读源上验证，不能用脚本回答或用户给定材料代替。飞书求值窗口标签的展示问题也未在本次修改。

## 后续真实模型对照

命令与环境准备见 [README](../README.md)。另获具体 Profile、端点、模型和费用授权后，基线和候选使用同一份评测文件、同一输入与工具集合，分别至少运行3次。保留采集器的干净源码与 harness hash 核验；只同步评测文件，不向基线产品注入新说明。

采集器可以从候选工作树提供同一份评测文件，通过 `PYTHONPATH` 首项选择独立干净基线的 `src`；其 `source_sha()` 会核对实际导入的产品路径与干净 Git 版本。不要把评测文件复制成基线的未提交修改，也不要覆盖基线产品说明。

逐项人工核对任务范围、证据含义、事实/推断/未知、时间与单位、是否无谓澄清/取数，并比较耗时和可取得的用量。无需逐字一致或固定工具顺序。正确指出证据不足是有效回答；已提供足够依据仍机械要求更多数据也应记录为问题。不能仅凭解释题通过就声称完整主机巡检通过。

本轮只获离线验证与审查授权；未调用真实模型、公司监控源或飞书，没有合并、部署。
