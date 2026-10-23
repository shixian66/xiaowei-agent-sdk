# 本地验证与 CI 小范围优化计划

> 状态：2026-10-23，用户已批准实施；纳入三点修订及本地实测基线、同环境收集两项补充。本文保留批准的实施要求；当前状态和验证证据由 AGENT_HANDOFF 及对应 PR 维护，避免为回写 CI 结果再触发全量。

**目标：** 缩短日常开发等待，保持现有默认 CI 的测试用例覆盖及失败阻断行为。

**基线：** `main@7a790c36b9be29080fdc896a48a6ed02aa79d332`。实施前核对最新主线、HEAD 和工作区；使用隔离主题分支，保留主目录与其他任务的修改。

**依据：** 本次对话确认的小范围优化及三点修订；验证规则唯一维护在 [AGENTS.md §6](../../../AGENTS.md#6-按风险验证与交付)。复用现有 pytest、uv 和 GitHub Actions，不增加依赖。

## 1. 范围与新增限制

实施修改四个现有文件：

| 文件 | 修改职责 |
| --- | --- |
| [README.md](../../../README.md) | 验证命令与权威规则引用 |
| [.github/workflows/ci.yml](../../../.github/workflows/ci.yml) | 发行测试去重、两个慢任务的耗时统计 |
| [test_workflow_policy.py](../../../tests/security/test_workflow_policy.py) | 同步受审 CI 命令、摘要及相关注释 |
| [AGENT_HANDOFF.md](../../../AGENT_HANDOFF.md) | 实测结果、剩余慢点及下一步候选 |

**新增限制：无。** 沿用现有 CI 失败处理和安全校验。

**本期不做：** 测试并行、按变更路径跳过测试、缓存调整、测试平台或分流框架、业务代码及业务测试断言修改。这是本轮范围，不构成对后续方案的禁止。

## 2. 实施步骤

### 2.1 README 只维护命令

- 保留环境准备和完整回归命令，补充选择相关测试文件或用例的命令示例，保留 lint、type 命令；不再写一份“改什么、怎么验证”的规则表。
- 增加以下引用说明：

> 选用哪种验证见 AGENTS.md §6；日常改动不必本地全量，合并前由 CI 执行完整的默认测试集合。

- 将原有“integration 单独运行完整测试”的描述改为准确的任务分工，详见下一步。默认测试之外的浏览器、真实模型及公司环境验收不因这段说明变成已覆盖。

### 2.2 CI 发行测试去重并统计慢用例

- `integration` 改为以下命令，并同步步骤名称：

```bash
python -m pytest -q --ignore=tests/deployment --durations=30
```

- `p3-release` 从枚举四个文件改为负责整个发行测试目录：

```bash
python -m pytest -q tests/deployment --durations=30
```

- 保留现有九个 job 名称、触发方式、数据库隔离、失败退出语义及其他 job 的检查。普通测试和独立安全门禁的少量重复本轮保留。
- 在 `test_workflow_policy.py` 中同步上述命令、多行块和整文件摘要，并修正相关失效注释；保留其他安全校验，不以放宽断言使检查通过。

### 2.3 验证与交付

- 在同一代码版本、相同环境和默认 marker 条件下，三条命令均设置 `SDK_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres`，使用 `--collect-only -q` 收集三个命令的用例 ID：原全量命令、新 integration 命令、新 p3-release 命令。核对原全量集合等于后两者并集，且后两者无交集；核对 ID，不只比较数量。此次集合核对使用临时结果，不新增分流框架。
- 运行以下本地相关检查，不为本次配置和文档调整另跑一次本地业务全量：

```bash
uv run --locked --extra dev python -m pytest -q tests/security/test_workflow_policy.py tests/security/test_docs_command_consistency.py
uv run --locked --extra dev ruff check tests/security/test_workflow_policy.py
git diff --check
```

- 用最终提交的一轮完整 CI 验收：现有九个 job 均通过；确认无意外漏测、增加 skip 或 deselect；两个慢任务输出耗时统计。失败时定位原因，修复后验证受影响内容及最终提交。
- 对具体版本的实际差异做独立审查，重点检查测试归属、失败处理和 workflow policy 修改；测试通过不替代审查。提交和 PR 操作按届时授权执行，不由本计划推定合并、打包或部署授权。

## 3. 收益与验收口径

已核对的历史参照为 [PR #130 的 CI 39899853901](https://github.com/shixian66/xiaowei-agent-sdk/actions/runs/39899853901)，受测 HEAD `9e858e704bfd994e1770ee77b5d419ec124b62d2`，与上述主线的文件树相同：integration 测试执行 **31 分 26 秒**，p3-release 测试执行 **8 分 26 秒**。两项任务并行开始，不能将各 job 耗时相加当作用户等待时间。

用单独发行任务耗时作近似，去重后 integration **预计约 23 分钟，CI 等待预计减少约 8 分钟**。这是估算，尚未通过修改后的 CI 验证。交付报告按实际数字写，不使用“CI 大幅提速”等笼统结论。

本轮主要收益来自日常局部改动按影响范围完成本地验证。用户提供的本地历史实测基线为 **2028 秒（33 分 48 秒，约 34 分钟）、4900 个用例**；未提供该次提交 SHA，不能当作与当前版本、环境完全一致的性能基准。实际节省为原全量耗时减去本次相关检查耗时，不统一宣称每次节省 30 分钟。

交付记录最终 SHA、用例集合核对结果、本地检查命令及耗时、CI 链接、两个任务的 pytest 耗时、job 耗时和整个 workflow 从开始到完成的等待时间。排队时间另列；基线或运行条件改变时说明比较限制，不将全部差值归因于本次优化。

验收以覆盖保持、相关检查和 CI 通过、耗时证据可复核为准，不把估算的 23 分钟设为硬性门槛；收益不足时如实记录原因及下一步。

## 4. Handoff 必须记录的下一步

- 在 AGENT_HANDOFF 的当前任务快照中记录本地实测和下一步，并直接链接对应 PR 的运行证据。远端实测耗时、`--durations=30` 的主要慢点和独立审查结论在 CI 完成后更新 PR 正文，不为追记这些结果再提交一次文档触发全量；不复制 README 命令或 AGENTS 验证规则。
- “剩余慢点／下一步”明确记录：integration 当前按单进程顺序执行测试；本轮先采集数据，不加入并行。下一步主要候选是 **pytest-xdist**，结合慢用例分布，从少量 worker 验证耗时、共享资源竞争和稳定性，再决定是否采用及 worker 数量。

已检查的 PostgreSQL 夹具按用例建立独立数据库，MCP 夹具使用动态端口，提供了进一步试验的基础；尚未完整验证并行适配，也不能承诺按核心数等比例提速。本轮不新增 xdist 依赖、不改测试夹具，真实模型和公司环境的既有验收缺口保持原状态。
