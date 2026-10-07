# P3：Compose 部署与实战验收实施计划

> 版本 **v0.4（2026-10-07，记录已发布 amd64 版本及公司云桌面内网直连选择）**。P3-A 与 P3-B 已分别经独立审查随 PR #49、#50 合入，amd64 发布流程随 PR #51 合入。用户已在公司服务器拉取 PR #56 对应镜像并完成配置检查、Vertex `model check` 和首次空库初始化；当前 Web 仅在服务器本机可访问。用户选择由公司内网直接访问服务器 RFC1918 地址，不增加应用层来源 IP 白名单。本文的原 P3-A loopback 证据保留为旧发行版本的验证记录；当前直连路径须由本次变更和公司云桌面实测单独验收。

**Goal：** 把已实现的小维交付到公司 Linux 服务器，升级时复用原配置，提供简单维护与恢复方式，并补齐真实用户路径证据。

**Architecture：** 沿用单进程小维与 PostgreSQL 两个常驻容器。应用通过现有 SDK、治理、Adapter、Evidence 与 Session 路径运行；发行包和操作者配置分开，操作复用 Compose、正式 CLI 与 PostgreSQL 工具。

**Tech Stack：** 本仓库锁定的 Python 3.11、运行依赖、SQLAlchemySession/PostgreSQL、Docker Engine 与 Compose 插件；本片不升级 SDK、模型协议、StarRocks 驱动或飞书 SDK。

**Spec：** [ARCHITECTURE](../../../ARCHITECTURE.md#deployment-package) 是部署约定唯一来源；权限、自然语言行为、资源限制、群边界及恢复语义继续引用其 §5–§9。[DEVELOPMENT_PLAN §7](../../../DEVELOPMENT_PLAN.md#7-p3获准环境实战) 规定阶段退出条件，详细任务只在本文维护。

**基线与执行：** 基于本地保存的 `origin/main` `fb67cd243ed3ab2beebae3970b909535042259f6`；本次远端查询失败，未宣称重新核实远端 HEAD。前置阶段的离线合入与实战缺口见 [AGENT_HANDOFF](../../../AGENT_HANDOFF.md)。现行路线已把 P1/P2/P2.5/飞书单群的真实部分留到 P3，不再以前述真实验收已完成作为 P3 开始条件。后续沿用 Claude 实施、Codex 对候选 SHA 独立审查；逐片串行，不自行合并、部署或归档。

## 1. 最小范围与复用

用户确认公司服务器是 x86，且可从 `ghcr.io` 下载镜像；本轮发行目标据此定为 `linux/amd64`。公司服务器的 `uname -m`、Docker Engine/Compose 版本与端口仍须在实机核对；公司内网访问控制由公司环境负责，本项目不加来源 IP 白名单。服务器只运行发行包，不要求 Git、Python、uv、测试工具或构建工具；不建设多架构发布或离线镜像分发。

| 已有能力 | 复用位置 | P3 工作 |
| --- | --- | --- |
| 正式启动与装配 | `src/xiaowei/cli.py`、`runtime.py` | 镜像运行同一 `xiaowei --config … serve`，适配容器监听与关闭期限 |
| Web 与飞书单聊/单群 | `web.py`、`static/`、`feishu.py`、`channel.py` | 验证正式入口、SSH、真实 mention/群成员/回复及排队；不另建入口 |
| Agent、工具与数据 | `app.py`、`governance.py`、`sqlguard.py`、`starrocks*.py`、`evidence.py`、`session.py` | 原链路进入已配置目标；复用 P2.5 与单群固定样例，核实真实模型和目标差异 |
| 存储与维护 | `storage.py`、`channel_store.py`、`migrations/` | 复用维护命令、实例锁与事务；基线 schema v5，A 增加 v6 密钥绑定，B 不再改变 schema |
| 安装与验证 | `pyproject.toml`、`uv.lock`、CI 的 `product-entry`、现有相关测试 | 复用 wheel 与锁定运行依赖，增加发行物和真实容器边界检查 |

关键链路：`Compose → cli → runtime → Web/飞书 → ChannelService → Application → SDK Runner → function tool → GovernedTools/SQLGuard → StarRocksAdapter → Evidence/PolicySession → 当前权限验证 → 渠道回复`。维护路径直接进入现有 CLI；重发只读取并复核已保存结果，不运行模型或业务查询。

基线的根 `Dockerfile`、`docker-compose*.yml` 属于旧 M5，含旧 API/Worker/迁移入口，不能作为新产品部署入口。新部署资产单独命名；旧 CI 和旧源码的物理清理不混入 P3。

## 2. 交付接口与必要契约

### 2.1 操作者只维护配置，不维护源码

正式发行包展开到固定部署目录；升级前保留上一版发行包（至少含原 `compose.yaml`、`release.json`、`OPERATIONS.md`），不覆盖操作者文件。以下为接口约定：

| 文件或资源 | 归属与更新规则 |
| --- | --- |
| `compose.yaml` | 发行物；只有 `xiaowei`、`postgres` 两项服务，使用已验证的镜像 digest，无现场 `build`；部署参数用 `.env` 插值，操作者不改此文件；普通应用版本不改变 PG digest、项目标识或卷绑定 |
| `OPERATIONS.md`、`release.json` | 发行物；唯一运维步骤与机器可核对的版本信息 |
| `xiaowei.example.json`、`.env.example` | 无凭据模板；只用于首次配置，升级不得复制覆盖实际文件 |
| `xiaowei.json`、`.env` | 服务器上操作者创建并保留；不在发行包中，升级前后逐文件比较内容不变；JSON 只读挂载，凭据沿用 `env:NAME` |
| `certs/` | 操作者管理的只读 CA 目录，无自定义 CA 时为空；首次准备目录，路径可在 `.env` 调整，不自动生成证书 |
| PostgreSQL 命名卷 | 与镜像和发行目录的版本独立，初次部署确定绑定后沿用 |

`release.json` 只含代码 SHA、应用/PG 镜像引用、CPU 平台、应用 schema 版本、允许直接运行/迁移的旧 schema 版本和配置迁移说明；无真实配置或凭据。旧版与新版的该文件在停止旧服务前区分普通升级和迁移；记录缺失或与实际库不符时不能盲目普通升级。部署包以文件白名单构造，镜像基于 wheel、运行依赖和必需资源构造；验收还要检查归档与最终镜像的所有层，不能只凭 `.dockerignore` 或合并后的文件系统。

部署参数在操作者的 `.env` 维护，首版不增加 override 文件。它与 `xiaowei.json` 均保留；运维从固定部署目录使用该文件，避免同名 shell 环境变量覆盖。下列为全部支持调整的部署参数（业务配置仍在 JSON）：

| `.env` 参数及默认值 | 消费者与一致性要求 |
| --- | --- |
| `XW_WEB_PORT=8501` | 宿主发布、容器目标和 SSH 本地端口使用同一个端口；`config check` 核对 JSON `listen_port` 相等及 loopback 来源，避免静默覆盖 JSON |
| `XW_WEB_BIND_ADDRESS=127.0.0.1` | 宿主发布地址；默认 loopback，内网直连时可填服务器 RFC1918 IPv4；容器预检要求 JSON 同时含 loopback 健康检查来源和该地址对应的 HTTP 来源 |
| `XW_STOP_GRACE_SECONDS=120` | 应用 `stop_grace_period`；模板值须通过 §2.2 的停止上界检查，调大 JSON 停机/飞书期限后不满足上界则预检拒绝 |
| `XW_CONFIG_FILE=./xiaowei.json`、`XW_CERTS_DIR=./certs` | 只读挂到 `/etc/xiaowei/xiaowei.json`、`/etc/xiaowei/certs/`；相对路径以固定部署目录解析，宿主路径不存在即失败 |
| `XW_LOG_MAX_SIZE=10m`、`XW_LOG_MAX_FILES=3` | 两个服务的日志轮转上限；只允许有效的有限正值 |
| `XW_PROJECT_NAME=xiaowei`、`XW_PG_VOLUME=xiaowei-pgdata` | 分别用于顶层 `name:` 和卷的显式 `name:`，首次配置后保存；不得随发行目录或版本改变，演练用独立名字 |

宿主发布地址默认是 `127.0.0.1`；需要云桌面直接访问时，操作者可将 `XW_WEB_BIND_ADDRESS` 显式设为服务器 RFC1918 IPv4。此设置只控制应用容器的宿主端口绑定，不新增来源 IP 白名单或应用登录；PostgreSQL 仍无宿主发布端口。镜像固定入口复用同一 CLI，配置路径固定为 `/etc/xiaowei/xiaowei.json`，默认 `serve`；维护仍用 `docker compose run --rm --no-deps xiaowei config check` 或 `storage upgrade`。首次初始化先启动 PG 并确认就绪；离线预检不启动 PG，迁移前停止应用并取得现有实例锁。绑定挂载用长语法、`read_only: true`、`bind.create_host_path: false`，配置缺失不能变成 root 目录。Compose 采用 `init: true`；应用与 PG 分别有健康检查，应用用 Python 标准库探针，不要求 curl。健康探针始终走 loopback；容器预检同时核对 `XW_WEB_BIND_ADDRESS` 和浏览器的精确 HTTP 来源。相关字段以 [Compose services](https://docs.docker.com/reference/compose-file/services/)、[项目名](https://docs.docker.com/reference/compose-file/version-and-name/) 和 [卷名](https://docs.docker.com/reference/compose-file/volumes/#name) 的契约为准。

`.env` 由 Compose 读取，文件权限限制访问；应用的 `env_file` 承接配置引用的环境值，PostgreSQL 通过显式 `environment:` 只映射自己的初始化变量，不能对两者共用 `env_file`。端口与停机宽限还通过显式 `environment:` 把 Compose 实际插值结果传给应用，预检必须核对实际值，不能重新读取 `.env` 猜测它们；合成用例覆盖 shell 同名变量覆盖造成的不一致。镜像模式开关不从 `.env` 读取。JSON/CA 要能被镜像非 root 用户读取。模板说明 PostgreSQL 密码与 `XW_DATABASE_URL` 的一致性及 URI 编码；升级不轮换 `XW_DIGEST_KEY`。核验不输出 `.env`、完整 `docker inspect` 或展开凭据后的 Compose 配置；仅从合成配置或内存中的解析结果提取键名及非敏感结构。检查 `$`、`#`、空格和引号的合成值传递，遵守 [Compose 变量规则](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)。

### 2.2 容器网络与正式入口

基线 `ServeConfig.listen_host` 只接受 `127.0.0.1`/`::1`。**保留此限制**，JSON 写 `0.0.0.0` 在原生与容器预检中都拒绝。A 仅添加镜像内固定入口的薄封装，向共享 CLI/运行装配传递可信容器模式，只有此路径在创建 socket 时使用 `0.0.0.0`。原生公开 CLI 无该开关，JSON、`.env` 不能选择模式；把容器配置复制到宿主机运行仍只监听 loopback。封装不重做参数解析、配置加载、Agent Loop 或维护逻辑；镜像运行模式不是 Docker 网络隔离的证明。

监听接口与浏览器来源分开：原生 `_consistent` 保持既有 loopback 同址 HTTP 检查；容器路径要求 JSON `listen_port` 与发布端口相同，并包含健康探针使用的 `http://127.0.0.1:<XW_WEB_PORT>`。`XW_WEB_BIND_ADDRESS` 默认 `127.0.0.1`，也可显式设为服务器 RFC1918 IPv4；启用直连时 JSON 还须包含 `http://<XW_WEB_BIND_ADDRESS>:<XW_WEB_PORT>`。JSON `listen_host` 仍保持 loopback，容器内监听与宿主端口映射是两层设置。`secure_cookie=false` 配合当前 HTTP 入口。Compose 长语法的 `host_ip` 使用 `XW_WEB_BIND_ADDRESS`，`published` 与 `target` 都引用 `XW_WEB_PORT`；预检不一致即拒绝，健康探针仍使用 loopback。

**Host/Origin 不做身份认证。** 这些检查继续防止不匹配的浏览器来源和 DNS rebinding，但不限制客户端 IP；能访问已绑定地址的人可使用固定 Web 操作者身份。用户选择由公司现有内网控制访问范围，本项目不新增来源 IP 白名单或公网访问能力。`XW_WEB_BIND_ADDRESS` 仅接受 loopback 或 RFC1918 IPv4，不接受 `0.0.0.0`、其他公网地址或 host network；PostgreSQL 无任何宿主发布端口。原生入口仍只允许 loopback，不支持公网多用户 Web。

运行单应用进程，非 root、只读配置、受限日志，复用实例锁。应用重启策略固定有限 `on-failure:3`，维护容器用 `run --rm` 不自动重试；schema/密钥不匹配的启动失败不得无限循环或把日志刷满。`init: true` 负责转发信号与回收子进程，不能代替应用停止协议。基线 CLI 已在调用 `runtime.serve` 前注册 SIGTERM；但设置 stop 不等于装配/恢复已立即停止，迁移也没有这条 serve handler。

A 必须按实际顺序计算停止上界：飞书 drain、Uvicorn 平滑停止、消费者/结构刷新取消、长连接停止、实例锁后端确认与 engine dispose。并行阶段不能机械相加，也不能把没有 timeout 的阶段写成有界；必要时只在这些现有退出路径补有界等待，超时受控失败且不能把未知请求当成功。`config check` 验证 `XW_STOP_GRACE_SECONDS` 大于该上界并留有余量；120 秒是待 A 验证的模板默认值，不能直接当成证明。首次恢复和 `storage upgrade` 中途 SIGTERM 要单独验证：不继续开始新业务，事务提交或回滚可判定，重启不自动重放。

`/healthz`、`/readyz` 复用现有进程、存储/实例及渠道语义；不新增 StarRocks 目标探测或付费模型探针。飞书不可用不等于 Web 不就绪；readiness 通过不证明全部目标可用，不能据此反复重启服务。

### 2.3 升级、配置兼容与恢复

- 普通升级：保存上一版发行物与镜像引用，核对新旧 `release.json` 的 schema/配置兼容说明，保留 `xiaowei.json`、`.env`、CA 和卷绑定；拉取应用镜像并完成配置预检后，只重建小维容器。模板不能覆盖配置；失败时按旧发行文件与原配置回退并检查就绪，不自动退回后再重放请求。PostgreSQL digest（包括同一大版本补丁）本次都不更换，另行验证。
- 含应用 schema 变更：先备份配置与完整应用数据库，再停止小维，使用新镜像显式执行 `storage upgrade`，成功后启动。沿用实例锁和迁移事务；普通 `serve` 不建表、不升级。旧程序拒绝新 schema 时，通过匹配的旧镜像、旧配置及升级前备份回退，不实现降级迁移。
- 配置兼容：保持现有配置含义和权限范围，不扩大权限或静默覆盖。增加只读 `xiaowei --config <路径> config check`，复用 `load_config` 核对配置、所需环境值存在且非空、本地 CA 可读；容器路径另核对 §2.1 的端口/来源和停止上界，容器 `serve` 复用同一校验，避免预检后改配置绕过。`config check` 全程不连接数据库、模型或飞书，不建表/写文件；成功 0，配置问题 2，仅报告字段路径和固定原因，不输出任何配置值。通过一次性容器在替换旧服务前检查；它不证明数据库版本、密钥绑定或外部服务可用。
- 备份恢复：配置（含密钥）与数据库按同一次备份成对保管。数据库覆盖 SDK 与全部应用表，使用 PG 镜像内同版本 `pg_dump -Fc`；宿主输出先写临时文件，明确检查进程退出码，成功才原子改名，失败不留下有效备份标记。恢复用 `pg_restore --exit-on-error` 进入新建隔离库，不覆盖现有业务库；验证 schema、密钥绑定及业务边界后，停止原服务，再由操作者修改 `.env` 的 `XW_DATABASE_URL` 指向恢复库，启动并回读，失败恢复原引用，保留两库。参见 [pg_dump](https://www.postgresql.org/docs/16/app-pgdump.html)、[pg_restore](https://www.postgresql.org/docs/16/app-pgrestore.html)。
- 恢复验证沿用架构 §8–§9：未完成请求不补跑，写入不明的会话关闭，过期或撤权内容不能读出；群中仅排队未开始的请求按现有语义中断。备份文件同样限制访问和保留，配置与凭据备份不进入 Git、CI 产物或对话。首版不提供自动备份、时间点恢复或高可用承诺。

### 2.4 摘要密钥与存储版本

B3 采用数据库指纹校验方案（设计权威见架构 §10），不只依赖操作说明或文件比较。基线 `ChannelStore._owner/_request_key` 都依赖摘要密钥，当前没有校验，换密钥会改变会话与去重键。

- A 新增应用迁移 v6：一个安装元数据记录保存带用途区分的固定标签 `xiaowei:storage:digest-key:v1` 的 HMAC-SHA256 十六进制指纹；指纹格式版本为 1。保持现有密钥解析/字节编码及会话/请求摘要算法不变，不修改 SDK 表。具体表名与内部函数由实施决定。
- 全新 `storage init` 在既有实例锁保护下，把应用建表和首次指纹写入放在同一事务；已安装数据库的 init 只能检查，不能重写指纹。v6 指纹缺失、多条、损坏或不匹配均返回受控运行失败（退出码 1），不输出密钥或指纹。
- `serve` 在恢复、接收请求和任何模型/StarRocks/飞书 I/O 前验证；`storage upgrade` 在变更前验证已有指纹，`storage cleanup` 在删除前验证，`requests resend` 在装配外部发送通道前验证。拒绝路径除必要 PostgreSQL 检查外零外部调用、零业务写入；不能清空库、覆盖指纹或自动重放来恢复。`config check` 仍为离线检查，不冒充此验证。
- v4/v5 尚无指纹，`storage upgrade` 默认拒绝首次绑定；仅为此迁移提供 `--bind-existing-digest-key`，操作者须先确认原部署仍使用的配置/密钥并成对备份。该标志是一次性人工确认，不是旧密钥正确性的技术证明；来源不明、原配置丢失或无法核对时保持旧库不迁移，不能从新模板生成密钥后使用该标志。迁移和首次指纹写入共用实例锁与同一事务；标志不能修复/覆盖 v6 的缺失或不匹配指纹。此历史限制在运维说明中明确，首个全新 P3 部署不经过此路径。
- B 的普通版本升级固定为 A 的 v6 镜像 → B 的 v6 镜像；迁移演练另覆盖旧 v4→v5→v6，恢复备份包含指纹。恢复后错配密钥必须在任何恢复动作/业务请求前拒绝；恢复正确配对后，旧会话仍可定位，重投已完成消息不再次运行模型/SQL。首版不提供密钥轮换或摘要重建工具。

## 3. 切片与验收

顺序为 **A → B → C**。A/B 使用合成数据与隔离服务；C 需要逐项获得真实环境授权。单片独立审查候选 SHA；后片沿用已通过的同一部署契约。

### P3-A：精简发行包与受保护的双容器启动

**可独立验证的结果：** 从发行包及预构建镜像启动正式 CLI/Web，配置与部署参数在容器外保留；首个镜像即具备 schema v6 密钥绑定，能作为 B 的升级起点。

**Files：** 新增 `Dockerfile.runtime`、`deploy/compose.yaml`、镜像专用的 `deploy/container_entrypoint.py`、`deploy/.env.example`、`deploy/OPERATIONS.md`、`scripts/package_release.py`、`src/xiaowei/migrations/006_digest_key_binding.sql`、`tests/deployment/test_release.py`/`test_compose.py`；修改 `.dockerignore`、`src/xiaowei/runtime.py`/`cli.py`/`storage.py`、对应 `test_runtime.py`/`test_cli.py`/`test_storage_v2.py` 及受影响的初始化调用、必要的 Web/正式启动用例、`.github/workflows/ci.yml`。配置模板继续取 `examples/xiaowei.example.json`，不改成 `0.0.0.0`，不另维护一份业务样例。README 在实现验证后链接唯一运维说明。

**接口：** 发行文件/参数、容器模式、预检与密钥绑定分别按 §2.1–§2.4。存储初始化/检查/升级接入同一指纹验证，新增迁移与一次性旧库确认标志；不改变请求摘要或 SDK Session 接口。打包脚本只在构建侧用标准库收集白名单与受测镜像引用，不传入真实配置、不登服务器或自动发布。

- [ ] 记录目标 CPU、Docker/Compose 版本、镜像仓库与端口；从现有 CI 的 wheel 安装方式构建仅含锁定运行依赖的镜像，固定受测应用与 PG digest，保留代码版本信息；只承诺本次实测的平台。
- [x] 先定义回归反例：复制配置到原生入口不允许外部绑定、原生/容器均拒绝 JSON `0.0.0.0`；升级后非默认端口/停止期限仍有效；换摘要密钥导致会话/去重键变化时必须在业务前拒绝。检查失败分支的实际 I/O 次数，不能以文件存在代替行为。
- [x] 发行归档核对白名单并加入仓库哨兵反例，哨兵不得入包。对最终镜像 `docker save` 的每一层逐层列成员并检查 `docker history --no-trunc`，不能先 COPY 再删来掩盖。排除五份项目根文档、项目 `docs/`/`tests/`、`xiaowei_agent` 与 `alembic`/`pytest`/`ruff`/`mypy`/`hatchling` 等开发包；第三方运行依赖自身携带的测试、许可证与必需元数据不算项目资产。核对 wheel 的 METADATA 未嵌入项目 README（当前 pyproject 未声明 readme，只作源码依据），静态页与全部迁移仍可读取。
- [x] 落实 §2.1–§2.3 的最小 Compose 与预检。用非默认端口、CA 路径、日志上限与较长停止期限验证不用改 `compose.yaml`；探针与服务使用同一 JSON。挂载源缺失时失败且宿主不产生同名目录；合成特殊字符凭据原样传递；解析 Compose 仅核对键名/结构，PG 无模型、飞书、摘要密钥等应用变量。
- [x] 配置预检验证零外部 I/O、文件内容不变，缺/空环境值、不可读 CA、端口或停机宽限不一致均退出 2，输出无任何配置值。普通 `serve` 在空库、schema 错配、密钥错配时退出 1；按实际 restart 策略确认有限失败而非无限刷日志。缺配置应在绑定挂载或配置校验阶段失败，不生成默认文件。
- [x] 用真实隔离 PG 验证全新 v6 初始化、重复 init、已有 v6 指纹缺失/损坏/错配，以及 v4/v5 首次绑定的默认拒绝与显式事务路径。逐一覆盖 serve、upgrade、cleanup、resend；拒绝不恢复/删除/写业务记录、不更新指纹且无外部服务调用。SDK 表仍由原公共接口建立。
- [ ] 正式 HTTP 验证首页、就绪、Cookie/Host/Origin；实查宿主发布地址与所选 `.env` 一致，PG 无宿主发布端口。默认 loopback 路径用 SSH 转发；公司云桌面直连路径须在云桌面实际打开配置的 RFC1918 地址并完成浏览器流程。公司网络访问控制由公司环境负责，本项目不测试或配置来源 IP 白名单。
- [x] 根据源码与故障注入记录完整停止上界，核对 §2.2 的宽限校验；正常负载、长请求、飞书关闭慢、锁后端释放慢均在期限内结束或受控失败。验证 `init: true` 转发 SIGTERM，覆盖 Uvicorn 启动前的恢复及 `storage upgrade` 中途停止：事务全有或全无、锁释放可确认、下一次启动不重放，不能仅测服务空闲停机。
- [ ] 在 CI 添加新产品发行检查，与旧 `compose-smoke` 分开记录；交付打包清单、镜像平台与 digest、候选 SHA 及命令结果，接受独立审查后交接 B。

**成功判据：** 不依赖源码仓库安装；发行物及最终镜像层符合白名单，资源齐全；默认仅经宿主 loopback 可达，显式直连时只绑定指定 RFC1918 IPv4；原生边界不变；部署参数不用改发行文件，错密钥在业务前拒绝。保留 A 镜像 digest/归档作为 B 的真实升级输入，不将首次部署或同版本重建称为版本升级。

**关键失败：** 挂载权限错误或路径不存在不能静默创建默认配置；凭据特殊字符不能被改写；HTTP 来源错误被拒；健康探针不能因为 Host 写错永远失败；同库第二实例被拒；坏包或坏镜像不覆盖用户配置。

**P3-A 实施证据（修订 head `9f69beba19736d293c22d50a59b50c90ac7f0d4e`，已复审并随 PR #49 合入）：** 本机 Docker Engine 29.5.2、Colima Linux/arm64 与独立 `docker-compose` 5.5.1 上，发行归档白名单和哨兵反例、镜像逐层审计、正式两容器入口、非默认参数、特殊字符环境值、loopback 发布、PG 零宿主端口、HTTP 成功/拒绝、正常 SIGTERM、有限重启、v6 密钥绑定和迁移中断回滚均已实际运行。首次独立审查在 `4867801` 发现停止上界漏算消费者取消期限、原生绑定缺少直接回归；修订加入 12 秒有界取消并计入宽限，直接断言 runtime/CLI 原生绑定，覆盖长请求停机及结构刷新、模型客户端、实例锁、连接池、Web 和锁监视关闭超时，相关超时与 m10 变异均被用例快速抓住。模型与 StarRocks 使用不可达本机地址或既有驱动替身，飞书使用真实产品路径下的渠道替身；这些结果不证明真实外部服务。该段记录完成时，P3-C 尚未开始；现行发布和公司服务器状态见下方补充及 [AGENT_HANDOFF](../../../AGENT_HANDOFF.md)。

**2026-10-07 访问路径决策补充：** 用户在公司云桌面用 `http://172.20.0.8:18501/` 访问时收到连接拒绝；服务器本机 `/readyz` 为 200，而 Compose 仍将端口发布到 `127.0.0.1`。用户明确选择直接访问服务器内网地址，并由公司现有网络控制访问范围。本次实现增加 `XW_WEB_BIND_ADDRESS`（默认仍为 loopback）、RFC1918 地址与精确来源的一致性预检，并将应用健康探针超时调整为 20 秒（服务器实测探针约 10.6 秒，原 6 秒超时；用户手工改为 20 秒后健康）。原记录中的“另一台同二层主机不可达”属于旧 loopback-only 验收门槛；按用户当前选择，不再作为本项目的验收项，改为在公司云桌面验证直连成功。新路径尚无公司云桌面实测；默认 loopback、原生绑定和 PostgreSQL 不发布的旧证据仍适用于默认路径。

### P3-B：保留配置的升级与基本恢复

**依赖：** A 通过独立审查且保留其实际镜像与发行包。**结果：** 运维按一份说明完成 A→B 普通升级、失败回退、旧 schema 迁移、配套备份恢复与请求编号排障。

**Files：** 继续完善 `deploy/OPERATIONS.md`、`tests/deployment/test_compose.py`，新增 `tests/deployment/test_maintenance.py`；必要时补已有 `test_storage_v2.py`/`test_runtime.py` 的具体回归。正常情况下不新建产品维护模块或迁移框架。

**接口：** 沿用 A 的 v6、指纹门禁、维护 CLI 与退出码 0/1/2；PG 工具在 PG 容器中运行。B 不再改变 schema，不为验证改 SDK 私有表结构或新增维护框架。

- [x] 在 A 镜像上建立合成个人/群会话、请求与证据，用非默认部署参数运行；保存旧发行物，升级到 B 候选镜像（独立构建、不同 digest、同为 v6，不能只改 tag），逐文件核对 JSON/.env/CA，逐项核对端口、停止宽限、日志、项目/卷和探针。实证旧会话可继续、完成消息重投模型/SQL 不重跑；做不到不同 digest 升级时此项保持打开，不能用同版本重建或 C 的首次安装替代。
- [x] 故意使 B 配置预检失败或拉取失败，旧服务继续运行；再演练切换后的启动失败，恢复原 `compose.yaml`、`release.json` 与原配置，按保存的 A digest 回退并回读。保留上一版发行包与镜像直至新版本接受，不依赖已经覆盖的文件或可变 tag。
- [x] 容器迁移链分开留证：用 v4 基线 `a938a0f1b481f1cb23507dc170380c73028b8e54` 与 v5 基线 `fb67cd243ed3ab2beebae3970b909535042259f6` 的 wheel 构造仅供演练的旧版本镜像，按既有用例先实跑 v4→v5，保留个人记录后补群样本；再用 A/B 的正式 v6 镜像执行 §2.4 的显式首次绑定与 v5→v6。旧镜像是测试夹具，不冒充历史正式发行；旧个人配置不注入群字段。验证在线迁移/未知或更高版本被拒、失败完整回滚、旧程序拒绝新 schema。
- [x] 把需要迁移的库误走普通 `serve`，验证退出 1、重启次数有限且未自动迁移；运维必须在停旧服务前从旧新发行元数据判断升级路线，库状态不明先核对，不能以离线预检代替 schema 判断。普通 A→B 不运行迁移；需要回退 v6→v5 时用匹配的旧镜像/配置与迁移前备份，保留升级后的库。
- [x] 用 PG 自带 `pg_dump`/`pg_restore` 演练临时文件失败与成功改名、隔离库恢复、当前 `.env` 的 `XW_DATABASE_URL` 切换与失败恢复原引用。dump 后在原库写入恢复库没有的标记；同时核对 Compose 合成环境、探针的 `current_database()` 和 `pg_stat_activity` 中运行中 serve 的 TCP 会话数据库，证明切换到恢复库、回切后重新连接原库。检查 SDK/应用表、指纹、过期/撤权及群/个人归属；错配密钥在 serve/upgrade/cleanup/resend 均拒绝且零业务 I/O，正确配对后旧会话和去重仍成立。首次绑定历史密钥的人工前提不能由合成用例替操作者证明；来源不明不执行。回退可能丢弃备份时点后记录，需在操作前说明。
- [x] 通过一次受控存储故障/进程中断验证请求编号与恢复行为：未完成请求不重跑模型或 SQL，已保存结果的重发重新查权限与群成员资格；清理只处理过期且可清理对象。检查日志无正文/SQL/结果/凭据，轮转有上限；容器停止期限足以完成既有停止顺序。
- [x] 运维说明只保留首次配置、启动、普通升级/回退、含迁移升级、停止、状态/排障、清理、配套备份恢复等实际命令；在空目录和已有配置目录各走一次，明确有限重启策略下主机重启后的启动操作、PG 任意 digest 变更另行验证。再次通过 config check 的四项契约：零外部 I/O、文件不变、配置错误退出 2、输出无值。本机只实测指定小维服务的 stop/up；全栈停止或真实主机重启后的 `up -d` 留 P3-C。记录候选 SHA 与两版 digest，独立审查后交接 C。

**P3-B 实施证据（修订 head `64180239549d43a8c16994babc3ad794b1d0511a`，已独立复审并随 PR #50 合入 `9a1f5d522ea985eb1a21de812019717a0680cad3`）：** `tests/deployment/test_maintenance.py` 从 P3-A 精确提交与当前候选分别独立构建不同镜像；用正式 Compose/HTTP 入口实跑预检失败、拉取失败、A→B 只换应用容器、保存结果重投、错密钥有限重启、按旧控制文件和 A 镜像回退。另从锁定的 v4/v5 提交构造测试夹具，实跑 v4→v5→v6、普通启动不迁移、缺少首次绑定确认拒绝、未知/更高版本拒绝、旧程序拒绝 v6，以及迁移前备份恢复给 v5 旧程序。`pg_dump -Fc` 失败不留归档、成功后原子改名，`pg_restore --exit-on-error` 恢复到隔离库。首次独立审查发现替代 `--env-file` 不会覆盖 Compose 服务的 `env_file: ./.env`，旧用例又因两库内容相同不能发现误连原库；修订改为备份并实际替换当前 `.env`，dump 后给原库加独有标记，核对 Compose 合成 URL、探针数据库名及 `pg_stat_activity` 中 serve 的 TCP 会话数据库，且明确验证回切原库。错误恢复 URL 变异会在 serve 会话数据库断言失败。切换后 SDK/应用表、指纹、个人/群归属、过期 Evidence、旧会话与去重保持。请求中断、重发权限/群成员复核、清理竞争、日志脱敏与停止上界沿用同一候选上的既有回归并纳入必需检查。外部模型、StarRocks 和飞书未调用；合成 Evidence 只证明保存与恢复，不证明真实授权复核。实际全栈/宿主机/Docker 重启、公司网络和获准镜像仓库仍属 P3-C 环境证据；PG 镜像 digest 变更仍不在范围。

**成功判据：** 普通升级在保留前版并核对兼容说明后，按“拉镜像 → 只读预检 → 只重建小维”完成；不用重填业务/部署参数或重建库。不同 digest 升级与回退均有证据，恢复的密钥/数据库配对且边界继续生效；运维不用掌握 Python、Git 或迁移内部实现。

**关键失败：** 新版配置校验失败时保留旧服务；拉取失败不停止旧服务；升级或恢复出错不给成功结论；不通过清空卷、重新生成密钥、自动重跑请求“修复”问题。宿主机故障后的卷和备份可用性分别说明，不把持久卷当备份。

### P3-C 前置：私有 GHCR 的 amd64 发行流程

**批准依据：** 用户确认公司服务器为 x86、允许从 `ghcr.io` 拉镜像，并授权准备发布流程。此项只解决获取可安装版本的路径，不代表真实服务验收完成。

**结果：** 管理员可从 `main` 手动启动单一 `linux/amd64` 发布。流程使用现有 `Dockerfile.runtime` 构建应用镜像，以 Buildx 返回的 digest 调用 `scripts/package_release.py`，将五项白名单部署文件附加到同一代码 SHA 的 GitHub Release。PostgreSQL 继续使用 P3 已验证的 16.15 镜像 digest。服务器通过单独的 `read:packages` 凭据拉取私有应用镜像；不克隆代码或在服务器构建。

**Files：** 新增 `.github/workflows/publish-amd64.yml` 与 `tests/deployment/test_release_workflow.py`；扩展 `tests/deployment/test_release.py`、`.github/workflows/ci.yml` 和 `tests/security/test_workflow_policy.py`。更新唯一安装步骤 `deploy/OPERATIONS.md` 与受影响的架构、路线、README 和 handoff 状态。

- [x] 定义仅 `workflow_dispatch` 且只接受 `main` 的发布流程；`contents:write` 只在发布 job，GHCR 写令牌只传给登录步骤。流程设计为构建、拉取精确 digest 并以正式镜像入口运行 `xiaowei --help` 后才生成归档和 GitHub Release 资产。
- [x] 为发布触发、主线保护、amd64 平台、锁定 PG digest、独立 GHCR 凭据和精确 digest 发行包增加静态契约回归，并纳入 CI 的 P3 部署检查。
- [x] 仓库管理员设置 GHCR 发布凭据并手动运行 workflow；`publish-amd64` run `37605907034` 成功，镜像 digest 为 `sha256:6a65880394c24e31061cf5b0dd10ea38019611ba0a7c3e675cd014546165acc9`，发行包 SHA256 为 `dbc2d0e879b9e5f5ab7aa10e00bbeeabf8d7db8e5125be5d01684595ed9dfda9`。用户已在公司服务器成功拉取该应用镜像；是否在 GitHub 页面核实 Package 为 Private 尚无记录。

**尚未验证：** 本工作树是 Mac ARM，不能复现 amd64 主机运行；GHCR Package 的 Private 可见性没有核实。新版本在公司云桌面的 Web 直连与真实查询流程仍待验证。

### P3-C：获准环境实战与用户接受

**依赖：** A/B 通过，amd64 GitHub Release 与私有 GHCR 镜像已产生，§5 的真实环境前提齐备。**结果：** 同一个候选 SHA/镜像在公司服务器上由真实用户完成业务任务，关闭对应前置阶段的实战缺口。

**Files：** 使用 `tests/sdk_core/gate0.py` 的既有固定样例及 `scripts/gate0_real_model.py`；需要扩展样例运行时只补该现有脚本，不建评测平台。正式入口实战证据单独记录在 `docs/acceptance/p3-live.md`，完成后 handoff 仅保留状态与链接；详细任务仍只在本文。此证据文件不进入发行物。

- [ ] 固定代码 SHA、镜像 digest/平台、配置非敏感摘要、目标版本及 Model Profile；C 每项证据绑定同一候选镜像 digest，若候选变更须重验受影响项并核对其余证据适用性。首次部署只算首次安装，不关闭 B 的版本升级。先核实授权、只读账号实际权限/默认角色、资源组与大查询限额，再开放相应目标；不能用本机 4.1.4 实验代替公司目标版本。
- [ ] 按 A/B 的说明部署，检查容器、挂载与 PG 无宿主发布端口。默认选择 SSH 转发，或在公司云桌面直连用户选择的 RFC1918 地址；当前待验收路径为浏览器访问 `http://172.20.0.8:8501/`。在真实浏览器完成首次消息、结果展示、追问与新建会话；不从 helper 直接调用成功推断浏览器成功。公司网络访问控制不由本项目配置或验证。
- [ ] 先以一个获准 Profile 运行既有任务正反例，记录工具选择、实际业务查询次数、结果事实、依据、误执行样例、步数、耗时及可得用量。自然语言“不执行/只解释/只写 SQL/裸 SQL/指代不清/多人历史/注释注入”不得因历史或注入自动查询；必交正例可完成，反例中的错误查询必须修复或重新明确产品边界后再接受，不能用平均成功率掩盖。
- [ ] Web 与真实飞书单聊均完成查结构、查询、连续追问、SQL/计划/表布局诊断；配置已有审计源的目标验证慢查询列表，无审计源的目标正确隐藏工具。按拟开放目标逐一验证路由、当前权限和来源；至少两个不同的获准目标证明真实跨目标追问，三目标容量继续引用既有离线证据，环境不足则相应实战项保持打开。
- [ ] 用真实指定群的两名成员验证 @准入、A 问 B 追问、同群串行、等待提示、原消息回复、满队列/等待超时、成员退出、原消息撤回、重投与重启；与 Web/单聊交叉检查数据归属。对发送结果不明如实记录，不重跑 Agent 或 SQL。
- [ ] 在获准预算内检查正常/无数据、危险 SQL、撤权、外部故障、慢工具超时、进程重启与资源超限。用代表性正常 SQL 与受控超限对照验证 DBA 限额生效；不擅改公司的权限、资源组、FE 配置或安装审计插件。再用发布镜像执行 B 的最小恢复演练并用请求编号定位一次失败。
- [ ] 报告逐项绑定 SHA、镜像 digest、Profile、目标版本、入口与采集时间；只保存获准脱敏证据。技术审查与用户实用验收分别记录，用户明确接受后才结束 P3；部署成功本身不关闭模型质量、目标权限/资源、恢复或飞书实战项。

**成功判据：** 实际用户能经 Web 和飞书完成上述任务，来源/事实正确，限制说明清楚；所有拟开放能力有对应证据。真实模型的有限样例通过不构成所有自然语言永不误执行的保证，按架构披露残余。

## 4. 检查与审查重点

本轮只检查文档差异、相对链接、源码对应事实和现有适用的文档检查。以下命令是实施后的计划，新增文件当前不存在；不把计划命令写成已通过。

| 切片 | 最少检查与成功输出 |
| --- | --- |
| A | `uv run --locked --extra dev python -m pytest tests/deployment/test_release.py tests/deployment/test_compose.py -q`；受影响的 `test_runtime.py`、`test_cli.py`、`test_storage_v2.py`、Web/正式启动及存储初始化调用检查；发行镜像内 `xiaowei --help` 与资源加载；`docker compose config --quiet`、最终镜像逐层检查和发布地址核对。直连路径在 P3-C 由公司云桌面浏览器实测；不测试同二层其他客户端或配置来源 IP 白名单 |
| B | `uv run --locked --extra dev python -m pytest tests/deployment/test_maintenance.py -q`；同候选镜像执行现有 CLI 与真实 PG 备份恢复。只允许临时独立项目/库/卷；检查配置不变及外部执行次数，退出无残留测试资源 |
| 源码受影响时 | `uv run --locked --extra dev ruff check src/xiaowei tests/deployment tests/sdk_core/test_runtime.py`、`uv run --locked --extra dev mypy src/xiaowei`，再运行受改动影响的现有检查；不反复扩大到旧系统全量测试 |
| C | 获准模型固定样例、正式 SSH/浏览器与飞书路径、目标权限/资源对照、真实部署恢复与用户接受；每项有实际证据，无环境则待验证 |

**Review Focus：** (1) 镜像专用绑定、原生 loopback 与配置的宿主发布地址一致，Host/Origin 不被当成认证；内网直连由公司云桌面实测，公司网络策略不纳入应用实现；(2) 操作者参数在 A→B 后保留，最终镜像各层不夹带开发资产/实际配置；(3) v6 密钥绑定覆盖所有运行/维护入口，旧库首次绑定的人工前提不被隐藏，失败不重放请求；(4) 目标权限/资源与版本差异；(5) 群投递/自然语言反例不被离线通过数量代替。分别在 A、A/B、A/B、C、C 验收；独立审查按确切提交给出触发、影响和依据。

## 5. 环境与尚未关闭的事实

| 必须确定的事实 | 何时关闭；缺失的影响 |
| --- | --- |
| Linux CPU 架构、Docker Engine/Compose 版本、端口及目标目录 | 首次部署前用只读系统信息确认；用户已确认 x86，且当前服务器 Compose 可拉取并运行该发行。新发行仍须在目标主机核对具体版本与端口；不自动升级公司的 Docker |
| 获准镜像仓库地址、拉取权限、构建/发布责任方 | 用户已确认服务器可访问 `ghcr.io`；`linux/amd64` 发布成功且服务器已拉取精确 digest。GHCR Package 是否标记为 Private 尚无记录，但不阻断已经验证的拉取路径 |
| 首个部署的配置文件、所需安全引用、现有卷/目录是否冲突 | 初次配置前确认；不能使用旧 M5 库或其他服务的卷。格式与挂载问题不靠覆盖配置解决。当前分支已实现容器宿主绑定地址配置，须合入并发布后才可用；旧发行不支持该设置 |
| 若接入旧 v4/v5 库，原密钥与数据库备份是否确实配套 | 仅旧库迁移前要求；新指纹无法反向证明旧密钥正确，需操作者从原部署核对。无法确认则不执行首次绑定、不动旧库；不阻塞全新 v6 安装 |
| 停止链路上界、公司云桌面的 Web 实际入口 | 停止上界已在 A 的离线与容器故障注入中验证；本次新增的内网绑定须在发布后从公司云桌面浏览器验证。按用户决定，本项目不测同二层其他主机的可达性，也不配置来源 IP 白名单 |
| Model Profile 的端点/模型/协议、数据接收范围与预算 | C 外发前明确；历史合成 Gate 0 成功不授予公司数据外发许可，不要求三家都通过后才能试用一个已获准组合 |
| StarRocks 版本、只读账号实际 SELECT 权限、默认角色、资源组/限额与业务口径 | C 开放每个目标前逐项核实；目标不满足时保持不可用。记录保留优先级低不改变 Evidence、权限与执行限制 |
| 飞书租户、测试单聊、指定群、两名成员及平台权限 | C 飞书验证前明确；沿用单群计划的真实平台缺口。群共享意味着当前群成员可见群内结果，不能用单聊授权推断群数据接收许可 |
| 配置/备份的存放位置、访问者、保留期及测试恢复库 | B 用合成数据完成；C 使用真实备份前由操作者确定。历史记录不要求自动恢复或严格 RPO/RTO，但不能自动删除现有记录 |

未列普通内部函数、测试夹具实现或额外组件设计。若真实验证发现功能缺陷，先说明现有契约哪里不成立，再在该片中修复最小问题；改变产品范围、权限或数据外发边界另行明确。
