# 小维容器运维

本说明适用于发行包里的 `compose.yaml` 与 `release.json`。部署只有小维和 PostgreSQL 两个容器；
Web 端口默认对服务器所有网卡开放，内网电脑用 `http://服务器IP:8501` 直接访问；PostgreSQL 不发布
端口。**Web 没有登录：能访问这个端口的人都以 `web.operator_id` 的身份和权限查询。** 访问范围由
服务器网络和防火墙决定；只想本机访问时在 `.env` 设 `XW_WEB_BIND_ADDRESS=127.0.0.1`。

建议使用固定目录 `/opt/xiaowei/current`。`.env`、`xiaowei.json` 与 `certs/` 由操作者持有，升级
只替换 `compose.yaml` 和 `release.json`；升级前留存的旧 JSON 配置（`.env` 的 `XW_CONFIG_FILE`
指向的文件，默认 `./xiaowei.json`）用于回退。旧发行包及旧应用镜像保留到新版验收完成。

## 获取 amd64 发行包

发行包是一个文件 `xiaowei-<完整代码 SHA>-linux-amd64.tar.gz`，向发布方索取。它已经带着应用镜像
（`xiaowei-image.tar`），服务器不需要登录任何镜像仓库，也不需要 Git 或 Python。PostgreSQL 镜像
按 `release.json` 里的固定 digest 从 Docker Hub 拉取。服务器访问不了 Docker Hub 时，在能访问的
电脑上准备。这个 digest 同时对应多个平台，ARM 的 Mac 不带 `--platform` 会拿到 arm64 镜像，所以
两条命令都要写 `linux/amd64`（`docker save --platform` 需要 Docker 28 或更新版本）：

```sh
pg_image='把 release.json 里 images.postgres 的值粘贴到这里'
docker pull --platform linux/amd64 "$pg_image"
docker save --platform linux/amd64 --output postgres-image.tar "$pg_image"
```

把 `postgres-image.tar` 传到服务器，`docker load --input postgres-image.tar` 后用同样的
`pg_image` 运行 `docker image inspect "$pg_image" --format '{{.Os}}/{{.Architecture}}'`，必须输出
`linux/amd64`；报找不到镜像时 Compose 仍会尝试联网拉取，不要继续启动。

先在服务器创建安装目录，再把发行包传过去：

```sh
mkdir -p /opt/xiaowei/current
```

```sh
release_sha=PUT_40_CHAR_CODE_SHA_HERE
server=SERVER_HOST_OR_IP
scp "xiaowei-${release_sha}-linux-amd64.tar.gz" "root@${server}:/opt/xiaowei/"
```

仅首次安装时展开到 `/opt/xiaowei/current`，核对镜像文件后导入：

```sh
release_sha=PUT_40_CHAR_CODE_SHA_HERE
tar -xzf "/opt/xiaowei/xiaowei-${release_sha}-linux-amd64.tar.gz" \
  -C /opt/xiaowei/current
cd /opt/xiaowei/current
grep image_file_sha256 release.json
sha256sum xiaowei-image.tar        # 必须与上一行的值相同
docker load --input xiaowei-image.tar
docker image inspect "xiaowei:${release_sha}" --format '{{.Os}}/{{.Architecture}}'   # linux/amd64
```

`docker load` 后镜像名是 `xiaowei:<完整代码 SHA>`，`compose.yaml` 已写好这个名字，并设为不从网络
拉取：没导入就启动会直接报找不到镜像。导入后可以删除 `xiaowei-image.tar`。

升级时把新归档展开到独立的 `/opt/xiaowei/releases/<完整代码SHA>`，在那里同样核对并
`docker load`，再按下文普通升级步骤替换控制文件。不要把未核验的归档或镜像标成已验收版本。

## 首次安装

1. 把发行包展开到固定目录，按上一节核对并导入应用镜像，确认 `release.json` 的平台与代码 SHA。
2. 仅首次复制 `.env.example` 为 `.env`、`xiaowei.example.json` 为 `xiaowei.json`，并创建
   `certs/`。以后不能用新包里的模板覆盖它们。
3. JSON 的 `listen_port` 与 `.env` 的 `XW_WEB_PORT` 使用同一端口。旧配置里的
   `web.allowed_origins` 已不使用，可以保留也可以删掉。自定义 CA 放在 `certs/`，
   JSON 写容器路径 `/etc/xiaowei/certs/<文件>`。
4. `.env` 只允许部署管理员读取；配置和 CA 须允许容器 UID 65532 只读。

两份配置的分工：`.env` 只放秘密和部署参数，可以用 `#` 写注释；`xiaowei.json` 放模型、StarRocks
地址、权限等非秘密内容，是标准 JSON，不能写 `#` 或 `//` 注释。

`xiaowei.example.json` 只有一个 Vertex 模型、一个 StarRocks 目标 `fat`、Web，飞书关闭（`null`）。
用户已批准将公司环境的非密钥配置值放进公开发行样例；复制到别的环境时先核对并修改模型 ID、
集群说明与 ID、FE 地址、默认库、只读账号、TLS 和审计源，再运行预检。样例的
`web.allowed_origins` 是旧版遗留字段，当前不限制来源。第二个目标和飞书需要时再按下文添加。
模型段里的 `vertex-main` 不需要填 API 地址、协议或输出方式，这些由程序固定，写了反而会被拒绝。

`.env` 和飞书片段里的 `<……>`、`cli_replacewithappid`、`oc_replace_with_chat_id` 是待填写标记；
旧版主模板的 `replace-with-approved-vertex-model` 与 `<……>` 也继续被预检拒绝。没替换的标记
会按字段路径报错，不会启动，且不回显原值；真实值里含尖括号不受影响。
`.env` 的 `XW_POSTGRES_PASSWORD` 仍是模板原文时同样拒绝。

| 位置 | 是否必填 | 填什么 | 从哪里拿 | 以后能否改 |
| --- | --- | --- | --- | --- |
| `.env` 的 `XW_POSTGRES_PASSWORD` 与 `XW_DATABASE_URL` | 必填 | 自己生成的数据库密码；URL 里的密码与它一致，特殊字符按 URI 编码 | `openssl rand -hex 24` | 初始化后不改 |
| `.env` 的 `XW_DIGEST_KEY` | 必填 | 自己生成的摘要密钥 | `openssl rand -hex 32` | 永不重新生成；与数据库成对备份 |
| `.env` 的 `XW_MODEL_API_KEY` | 必填 | Vertex AI 的 API Key 本身（Express Mode），不是服务账号 JSON 文件或项目号 | Google Cloud 管理员 | 可轮换，改后重启 |
| `.env` 的 `XW_STARROCKS_PASSWORD` | 必填 | 第一个目标只读账号的**密码** | StarRocks 管理员 | 可轮换，改后重启 |
| `.env` 的 `XW_WEB_BIND_ADDRESS` | 可选 | Web 发布到服务器的哪个 IP：`0.0.0.0`（缺省，内网都能访问）、`127.0.0.1`（只本机）或服务器某个内网 IP；不能写域名 | 服务器管理员 | 可改，改后预检并重建小维 |
| `.env` 的 `XW_ARCHIVE_STARROCKS_PASSWORD` | 按需 | 加了第二个目标时去掉行首 `#`，填它的只读密码 | StarRocks 管理员 | 可轮换 |
| `.env` 的 `XW_FEISHU_APP_SECRET` | 按需 | 启用飞书时去掉行首 `#`，填应用 App Secret | 飞书开放平台 | 可轮换 |
| `.env` 的 `XW_PROMETHEUS_READ_TOKEN` | 按需 | 选配需认证的 Prometheus MCP 时填写只读身份令牌 | 监控管理员 | 可轮换，改后重启 |
| JSON 的 `model.model` | 已填公司值；其他环境必改 | 获准的 Vertex 模型 ID，只含字母、数字、`.`、`_`、`-`；样例中的模型尚未在本次改动中实测 | 模型服务管理员 | 换模型后须新建会话，并重新运行 `model check` |
| JSON 的 `targets[].description`、`starrocks.target_id`、`host`、`database`、`user`、`tls` | 已填公司值；其他环境必改 | 集群用途与 ID、FE 地址、默认库、只读账号名及 TLS 设置 | StarRocks 管理员 | 可改，改后重启并按会话绑定规则新建会话 |
| JSON 的 `access.grants` 与 `web.operator_id` | 必填 | Web 操作者的内部 subject 及其工具 | 部署管理员决定 | 可改，改后重启 |
| JSON 的 `feishu` | 按需 | `null` 表示不启用；启用方式见下文“启用飞书” | 飞书开放平台 | 可改，改后重启 |

常见错误：`config check` 输出“仍是模板占位符”时按列出的字段替换；“引用的环境变量未设置或为空”
时补 `.env` 对应行；“vertex 的端点与协议由程序固定”时删掉 `model` 段里的 `base_url`、`api_mode`、
`output_mode`；“飞书用户 subject … 缺少有效授权”时在 `access.grants` 给该 subject 至少一个
工具，或从 `feishu.users` 删掉该用户。

```sh
cd /opt/xiaowei/current
chmod 600 .env
mkdir -p certs
docker compose --env-file .env config --quiet
docker compose --env-file .env run --rm --no-deps xiaowei config check
docker compose --env-file .env run --rm --no-deps xiaowei model check
docker compose --env-file .env up -d postgres --wait
docker compose --env-file .env run --rm --no-deps xiaowei storage init
docker compose --env-file .env up -d xiaowei --wait
docker compose --env-file .env ps
```

顺序是：填好两份配置 → `config check` → `model check` → 初始化数据库并启动。前一步没通过就不要
往下走。`config check` 不连接 PostgreSQL、模型、StarRocks 或飞书，也不写文件。它返回 2 时先修配置，
不要停止或替换正在运行的旧服务。`storage init` 只对全新、专用于小维的数据库执行一次。

`model check` 单独确认模型 Profile 和 Key 可用：它只读取 JSON 和 `model.api_key_ref` 指向的那一个
变量，不需要数据库、StarRocks、飞书秘密或 CA，也不连接它们、不写文件；但会向配置的模型发送一次
固定的合成提问并调用一个内置的假工具，**会产生模型调用和用量**。输出只有 Profile、模型名和
`valid`，失败只给类别，不显示提问、模型回答或 Key：

```sh
docker compose --env-file .env run --rm --no-deps xiaowei model check
```

| 退出码与输出 | 含义与处理 |
| --- | --- |
| 0，`model check valid profile=… model=…` | 模型完成了一次工具调用并给出合规的最终回答 |
| 2，字段路径与说明 | 配置或 Key 问题（未设置、为空、仍是模板），按提示修改后重试，没有发出模型请求 |
| 1，`model check failed: auth_failed` | Key 无效或无权使用该模型 |
| 1，`model check failed: rate_limited` / `upstream_error` / `unreachable` | 限流、模型服务错误、超时或连不上；稍后重试，不会自动重试 |
| 1，`model check failed: tool_not_called` / `tool_repeated` / `answer_invalid` / `model_failed` | 模型没按要求调用工具、重复调用、最终回答不合规、响应不符合协议，或模型客户端打开/关闭失败；该 Profile 不应投入使用 |

通过只说明这个 Profile 能完成合成数据上的工具往返和结构化回答，不代表正式提示词下的工具选择
与回答质量合格。

启动后在内网电脑的浏览器打开 `http://服务器IP:8501`（端口按 `XW_WEB_PORT`）。用 IP、域名或经
公司反向代理访问都可以，不需要在配置里登记地址。如果设了 `XW_WEB_BIND_ADDRESS=127.0.0.1`，改用 SSH
转发：

```sh
ssh -N -L 127.0.0.1:8501:127.0.0.1:8501 <服务器>
```

不要使用 host network。

## 增加第二个 StarRocks 目标

`targets` 里每个 StarRocks 集群一项。要加第二个集群时，把已有的那一项（从 `{` 到对应的 `}`）整段
复制一份，接在它后面并用逗号隔开，然后只改下面这些字段：

| 字段 | 改成 |
| --- | --- |
| `starrocks.target_id` | 新名字，例如 `archive`；1–32 位小写字母、数字、`_`、`-`，不能和已有目标重复 |
| `description` | 这个集群的用途，模型靠它选集群 |
| `starrocks.host`、`database`、`user` | 新集群的 FE 地址、默认库和只读账号名 |
| `starrocks.password_ref` | `env:XW_ARCHIVE_STARROCKS_PASSWORD`，并在 `.env` 去掉这一行开头的 `#`、填密码 |
| `starrocks.audit` | 新集群没有审计表时改成 `null`；这时在该集群上不能查慢查询 |

不再需要第二个集群时，把这一项和 `.env` 的对应行一起删掉。改完先运行 `config check`，通过后重启；
已有会话需要新建。

## 启用飞书

把 `xiaowei.json` 的 `feishu` 从 `null` 换成飞书段，并在 `.env` 填 `XW_FEISHU_APP_SECRET`。发行包
里的 `feishu-group.example.json` 是含指定群的完整飞书段（首次安装已展开在 `/opt/xiaowei/current`，
升级时在 `/opt/xiaowei/releases/<新 SHA>`）：把它的全部内容作为 `feishu` 的值粘贴进去，替换其中的
待填写标记；不启用指定群时把 `group` 改成 `null`。

`feishu.domain` 只能填 `"feishu"` 或 `"lark"`，省略时沿用原来的 `"feishu"`。看应用后台网址：
`open.feishu.cn` 对应 `"feishu"`，`open.larksuite.com` 对应 `"lark"`。这个字段同时决定
消息 API 和长连接端点。编辑后运行 `docker compose --env-file .env run --rm --no-deps xiaowei config check`，
再重建小维容器使配置生效。

如果 `/readyz` 显示 `"feishu":"unavailable"`，且启动日志为 `error_code=not_connected`，先只读核对
启动日志中的固定错误码、`xiaowei.json` 的 `feishu.domain`（不要输出其他配置字段）和应用后台网址。
`client_code=1000040351` 曾对应 Lark 应用使用飞书国内域名的报错；该整数码不能单独证明所有失败
都由域名导致。两处域名一致仍连不上时，保留错误码再排查，不在日志里打印第三方异常正文或凭据。
回退到不认识 `feishu.domain` 的旧镜像前，先从 `xiaowei.json` 删除此字段，再运行旧镜像的
`config check`；旧版的配置校验会拒绝新字段。

`feishu.users` 是允许单聊的名单，键是用户的 `open_id`，值是内部 subject。名单可以为空：为空时
单聊没有任何人获得权限；配置了指定群时，群成员仍按 `group.tools` 在群里使用，与单聊名单无关。

不知道某人的 `open_id` 时，让他直接给机器人发一条私聊文字。不在名单里的人会收到一条固定回复：

> 你还没有获得授权。你的编号是 ou_xxx，请把它发给管理员。

编号取自这条消息的发送者本人，不能由正文指定。这条回复不代表已获权：他发的任何内容和命令都
不会保存、不会交给模型或查询数据库。同一个人 10 分钟内最多收到一次，发送失败也算一次，不会
自动重发；重启小维后限额清空。日志只记 `unregistered`，不记编号。群消息、非文字消息、过期消息、
其他应用或租户的消息不会触发这条回复。

拿到编号后，给这个人开通单聊时，两处必须同时改：

```text
feishu.users["<该用户的 open_id>"] = "<内部 subject>"
access.grants["<内部 subject>"] = [该用户获准的工具]
```

subject 不能和 `web.operator_id` 相同；工具列表不能为空。改完先运行 `config check`，通过后重启
小维才生效。

### 飞书结果展示与容量

有证据的回答使用一张静态 JSON 2.0 卡片；建议、澄清和固定回执仍是一条文本。事实、来源、目标、
采集时间、固定说明与不完整提示默认可见；模型分析在事实之后。DDL、数据库值和模型文字只进入
纯文本组件，不解析 Markdown、链接、媒体或 mention。真正的 NULL 显示为 `NULL（空值）`，
字符串 `"NULL"`、空串与未提供字段分别显示。换行保留，不可见控制符显示为转义。
DDL 超限等非技术原因说明默认可见，并作为必要信息保留，不依赖可选原文 JSON；投影没有结果时
显示“（无结果）”，有表格但零行时显示“（无数据行）”。建议保留“未经系统核实”标签。

**客户端前提：** [官方 JSON 2.0 文档](https://open.feishu.cn/document/feishu-cards/card-json-v2-structure)
要求飞书/Lark 客户端至少 7.20，较旧客户端可能只显示标题。上线前由操作者确认桌面和移动端版本，
真实客户端展示仍需验收。不做版本探测；消息开始发送后不改发旧卡片或文本，以免重复投递。

同时遵守 `max_reply_chars`（200–3500）和完整请求体 UTF-8 的 **30,000 字节**上限。字符按显示
转义后的 Unicode 码点计数，包括所有可见及折叠文字、标题、列名、值、来源、说明、SQL、游标与
分析。字节包括卡片 JSON 编为字符串后、外层 HTTP 请求的再次转义，以及 UUID、
收件人或回复封装。任一上限先到就裁剪，不提高配置、不拆分成多条。

容量依次分给必要来源/限制、实际业务 SQL、整行事实、模型分析，最后才放其他技术详情。
实际业务 SQL 可折叠，但在结果行前分配容量；整条放不下时默认显示“SQL 未展示”。结构工具的
SQL 是采集模板，耗时是本次处理（含权限确认）耗时。原文 JSON 仅在剩余容量允许时提供，
不挤掉结果行。JSON/SQL 数据列及 DDL 本身作为结果默认显示。

“还有后续页”表示工具还有游标，不等于原始值丢失；真实工具截断、事实展示截断、分析展示截断
各自说明。表格最多五个、每表最多 50 列，额外或宽表按纯文本逐行字段显示；每张卡片最多 200
个组件。表格内每页十行，翻页只浏览已经随消息发送的行，不调用工具或续取数据库；工具续页由
Agent 在预算内完成。Web 本片保持原样，仍把有后续页标为“结果已截断”。

必要来源/限制放不下时，只发“结果未展示：单消息容量不足”，日志为
`reason=reply_capacity_notice`。说明成功记为 `sent`，只证明这条说明发送成功，**操作者的
`requests resend` 不能补发完整结果**；需缩小问题范围后用新消息提问。

本地渲染异常时日志为 `reason=render_failed`，未发起消息发送，投递记 `failed`、飞书 readiness
不锁住，操作者仍可重发；不打印异常正文。消息发送开始后的超时/取消仍可能已送出，按 `unknown`
处理，不自动重试。存储异常与投递权保护不变。上述路径仅经本机合成端点与隔离存储验证，
真实 Lark 收发、客户端效果、真实模型选择及公司 StarRocks 保持未验收。

## 选配 Prometheus 监控源

可选配官方 Prometheus MCP v0.18.0 的七项只读工具；旧的仅 `query` 配置继续可用。
MCP Server 单独部署在外部节点，Compose 不新增容器；小维只连固定地址。外部 Server 使用已核对的
只读工具集，不启用 TSDB 删除、reload、quit；需要规则读取时，锁版用例的 `--mcp.tools=list_rules`
已实测能开放规则与 core 读取。P2 离线实现已合入 PR #71，真实模型和目标 Prometheus 限额尚未验收。
保持现有 `xiaowei.json` 的其他字段，在顶层加入：

```json
"mcp_servers": [
  {
    "server_id": "prometheus-prod",
    "url": "http://prometheus-mcp.internal:8080/mcp",
    "auth_ref": "env:XW_PROMETHEUS_READ_TOKEN",
    "timeout_seconds": 5,
    "max_response_bytes": 64000,
    "allowed_tools": {"query": "prometheus.query"}
  }
]
```

这是现有 JSON 对象中的一个顶层字段片段，不要在对象外单独粘贴；地址只是示例，须换成实际固定端点。
若监控入口仅由已受限的内网访问且不需认证，可将 `auth_ref` 设为 `null`；若使用认证，令 `.env` 中的
`XW_PROMETHEUS_READ_TOKEN` 保存只读身份的令牌，JSON 仅留引用。官方 Prometheus MCP 会把小维的
Bearer `Authorization` 原样转发到 Prometheus，它不是与上游隔离的第二重身份。内网 HTTP 无须
配置 CA，但请求、结果与认证头以明文传输；只在可信内网和现有网络限制内使用。地址不能含用户名、
密码、查询串或片段，也不会跟随跳转。首选以外部 Server 的 `--prometheus.truncation-limit=200`
配合上例的 `max_response_bytes: 64000` 作为可截断读取的起点；P2 已用 3000 项合成数据和正式 Web 验证
标签发现、序列与 query/range 在这组值下返回部分结果并生成 Evidence。截断按行/条目计数，
接收上限按字节计数，两者不能相互换算；长标签或长 help 的源须按实际返回调小截断值或调整
接收上限后实测。不要直接关闭截断做全源发现。锁版完整截断标记和投影截断都会标为
`truncated`，Agent 可按需要收窄选择器、聚合或缩小窗口；部分结果不证明全量健康。
metadata 的全局值会作为上游 `limit` 发送，并非 MCP 本地按行裁剪；小维只开放非空具体指标名。
实际响应仍超过接收上限时整轮中止，不能将这类协议异常伪装为正常部分结果。

**规则读取须单独核对接收容量。** 锁版 `list_rules` 无过滤/分页参数，一次返回全部已加载规则，
不受 `--prometheus.truncation-limit` 约束，不带 Server 截断标记。上述 `200/64000` 起点没有覆盖
全部读取。启用前用同版 MCP 试读目标源，测量完整 MCP 响应的传输字节数；`max_response_bytes`
设为高于实测值并留余量，规则增长或变更后重新核对。若所需容量超过该源可接受的接收上限，
不在 `allowed_tools` 中选配 `list_rules`，也不加入相应授权。只测 Prometheus API 的 JSON 大小
不能代替 MCP 响应大小；小维接收后的投影截断不能缩小已经传输的响应。官方二进制与300条合成规则
已验证：全局截断值200仍返回全部规则，在64000字节接收上限下正式 Web 中止、零成功 Evidence。

如需发现或历史窗口，在同一 `allowed_tools` 中按需加入下面的准确映射；也可以只选其中几项：

| 远端工具 | 策略 ID | 适用任务 |
| --- | --- | --- |
| `range_query` | `prometheus.range_query` | 范围 PromQL 与趋势 |
| `label_names` | `prometheus.label_names` | 发现标签名称 |
| `label_values` | `prometheus.label_values` | 发现指标名、主机等标签值 |
| `series` | `prometheus.series` | 选择器匹配的序列标签 |
| `metric_metadata` | `prometheus.metric_metadata` | 指定非空指标名的类型/help/unit |
| `list_rules` | `prometheus.list_rules` | 当前已加载规则及求值状态 |

时间窗由 Agent 按问题选择；实际时间在调用前冻结，客户端边界与接口见 [P2 契约](../docs/superpowers/plans/2026-10-09-monitoring-mcp.md#31-p2-当前切片基线-4c5b89495fdf57bfd6d18bb4483d5682e56cce59)。
启用前核对 Prometheus **实际生效**的 timeout/max-samples/max-concurrency；客户端窗宽/点数不能限制
表达式内部扫描。即时 query 保留 Server 自己取当前求值时间的行为，采集时钟不是求值时钟。
metadata/rules 的官方返回会丢弃上游 warnings，页面保留固定限制说明，不能据此判定整体健康。
发现时优先用已知主机、job 或指标选择器收窄 `matches`；未知范围仍可做有限的源内发现。
已知指标可直接查询或读元数据，不要求固定先发现、再查询。

把每个选中工具的 `"<server_id>/<tool_name>"`（如 `"prometheus-prod/query"`）加入
`data_policy.model_tools`，再只给获准的
`access.grants["<内部 subject>"]` 加入同一工具 ID；Web 使用 `web.operator_id` 对应的 subject，
飞书单聊使用 `feishu.users` 映射的 subject，指定群使用 `feishu.group.tools`。不要因为配置了源就给
所有人加权限。空 `mcp_servers` 或不填该字段时，现有 StarRocks 行为保持不变。
`server_id` 代表一个稳定的监控源；改指向另一套监控数据时用新 ID，并重新核对授权。

修改后运行 `config check`：它核对结构、工具 ID 和已填写的认证引用，不连接外部 MCP，也不能
证明容器网络可达。重启小维后看 `/readyz` 的 `mcp.prometheus-prod`：`available` 表示启动发现
或重连核约成功，`unavailable` 表示启动时不可达或重连冷却已到期待下一轮，`disconnected` 表示调用
确认断线，`cooldown` 表示重连失败后暂缓尝试，`contract_mismatch` 表示选中工具契约不符。状态不回显
端点或凭据；单个监控源不可用时 Web 和 StarRocks 仍可用。查询用途里有该监控源授权的用户，
即使本轮只问 StarRocks，冷却到期时也可能在运行前等待一次重连；连接与列工具共用至多 3 秒的期限
（源配置更短时取更短值）。
重连失败后按该源从 2 秒起翻倍退避，最长 60 秒，成功后复位。选中读取遇到可分类的上游
401/403/5xx、断线或超时，该源本轮标为未核实，Agent 可继续调查其他已授权源；本源本轮不再
调用，原调用不自动重放。超时结果可能未知，不仅凭超时标记断线；未知错误或协议不合规仍中止
整轮。只有锁版 query/range 的确定解析错误可由 Agent 在额度内修正，失败仍计预算且没有成功证据；
全失败可给明确未经数据验证的建议，步骤耗尽会说明调查不完整。更改源地址或认证引用、撤权仍需停止旧小维进程、更新配置、通过
`config check` 后重启；旧监控事实的历史
读取和重发会按当前授权拒绝。改变模型可用工具的数据策略后，新调查按现有规则新建会话；旧获准 query
事实的历史读取不重新调用远端。回退到 R1 镜像须去掉它不识别的新工具映射和授权；回退到更早镜像前，
删除 `mcp_servers` 及其工具授权，再用旧镜像
`config check`，避免旧版拒绝新字段。

## 选配 Grafana 监控源

G3 已接入官方 Grafana MCP **v2.0.2** 的五项只读定义工具；实际公司 Grafana 版本/组织权限、
真实模型和双入口仍需验收。外部节点运行 MCP，小维 Compose 不新增服务。
固定 Grafana URL/组织和共享只读 Service Account；不启用通用 API、写工具或代理指标查询。
本机协议用例使用的开关为：

```text
--transport=streamable-http --enabled-tools=search,dashboard,datasource,annotations
--disable-write --disable-query --usage-stats=disabled
--allow-grafana-url-override=false --dynamic-multi-org=false
--server-auth-token=<从外部节点的安全运行配置提供，不写进群消息>
```

外部 Server 的 `GRAFANA_URL`、`GRAFANA_SERVICE_ACCOUNT_TOKEN` 属于 Grafana 连接；MCP 调用方
Bearer 是另一项凭据，不转发给 Grafana。内网 HTTP 沿用上节的运维取舍，无须增加 CA 管理。
在现有 `mcp_servers` 列表追加下面这一项，不覆盖原 Prometheus 配置：

```json
{
  "server_id": "grafana-prod",
  "url": "http://grafana-mcp.internal:8000/mcp",
  "auth_ref": "env:XW_GRAFANA_MCP_TOKEN",
  "timeout_seconds": 5,
  "max_response_bytes": 128000,
  "allowed_tools": {
    "search_dashboards": "grafana.search_dashboards",
    "get_dashboard_summary": "grafana.get_dashboard_summary",
    "get_dashboard_panel_queries": "grafana.get_dashboard_panel_queries",
    "list_datasources": "grafana.list_datasources",
    "get_annotations": "grafana.get_annotations"
  }
}
```

128000 是本机合成场景的起点，**不是全部实例的容量保证**。启用前测 `tools/list`、最大摘要及全部
面板表达式的实际响应大小；配套调整接收上限和现有四用途投影，过大时只选配已验证能读取的工具。
摘要/面板没有分页，超出接收上限仍中止本轮，不伪装成断线。
搜索与数据源目录的 limit≤100；搜索 page 从1开始，数据源 offset 从0开始，达到边界可按需续页。
搜索 total 是当前页数量；数据源 total 是筛选后总数；hasMore/投影省略都标明部分结果。
标记使用 Unix 毫秒、最多31天窗口和100条；这是本地容量边界，达到 limit 视为可能未完整，可分窗。
分页不等于服务端开销受限：数据源每次先读取全目录，摘要与表达式先读取完整 Dashboard。
初始化会读 `/api/frontend/settings`；首次 Dashboard 读取有 capability discovery。原生 API 按同源
settings 的 namespace 读取，未缓存 namespace 时还会读 settings；v2 对象可能先读 v1beta1 再读
storedVersion 的原生对象。数据源目录失败也可能读取同源 settings。这些是官方实现行为，不是小维重放调用。

按上节方式给 `data_policy.model_tools` 和获准用户/群加入 `grafana-prod/<工具名>`；群成员共享获准
源内的定义，不做对象白名单。仅填源地址不会授权。`config check` 后停止旧进程并重启生效；
`/readyz` 的 `mcp.grafana-prod` 复用同一源状态和重连机制，不代表 Grafana 上游权限或健康已核实。
历史与重发只做本地当前源/工具/接收权限复核；MCP 不可达不妨碍仍获准的历史，撤权则拒绝。
回退到不支持 Grafana 的镜像前，移除该源及其工具授权，用旧镜像运行 `config check`。

结果仅是定义/目录/标记，未执行面板查询。原始表达式中的变量/宏保持原样；panelId=0 表示读取全部，
官方结果没有逐条面板 ID。摘要未返回单位/阈值，visual-editor 的任意 target 不保留。
数据源 UID/名称/type 不证明它对应某个 Prometheus 源；没有可信对应关系时，实时指标按独立来源
报告，不声称来自这个面板。工具接口、失败分类与验收的唯一细则见[计划 §3.2](../docs/superpowers/plans/2026-10-09-monitoring-mcp.md#32-g3-当前切片基线-4cbb27a2b6e54050fc350fcd62a1a9e5cf123eae)。

## 选配 Alertmanager 监控源

A4 只读直连外部独立 Alertmanager **API v2**，本机以官方 **v0.34.1** 验证；公司实际版本仍须核对。
不安装额外服务，Compose 不变。在配置顶层选配下面这一段；URL 填根地址，可含部署子路径，
**不含 `/api/v2`**。用户名和密码只在运行环境的安全引用中填写，Compose 的 `.env` 会传给小维：

```json
"alertmanager_sources": [
  {
    "server_id": "alertmanager-prod",
    "url": "http://alertmanager.internal:9093",
    "username_ref": "env:XW_ALERTMANAGER_USERNAME",
    "password_ref": "env:XW_ALERTMANAGER_PASSWORD",
    "timeout_seconds": 5,
    "max_response_bytes": 128000,
    "tools": ["get_alerts", "get_alert_groups", "get_silences", "get_silence", "get_receivers", "get_status"]
  }
]
```

按需选 tools，再把 `alertmanager-prod/<工具名>` 加入 `data_policy.model_tools` 和获准用户/指定群
已有授权表；只填连接不授予权限。Basic 与 MCP 的 Bearer 是不同认证，不复用 `auth_ref`。
仅只读且已有网络限制时，两个引用可同时为 null。内网 HTTP 不需要 CA，认证头明文传输；
选择 HTTPS 时沿用正常证书验证。Basic 只验证调用者身份，不能证明账号仅可读；小维本片只发固定 GET。

API 没有服务端分页：先接收完整过滤响应，再用 limit（1–100）和 offset 本地分页；优先按已知
标签收窄 filters。receiver 是正则，布尔值表示包含相应状态，不是只选该状态。
128000 只是合成测试起点。启用前测代表性过滤结果与最大分组/静默目录的原始大小，配套现有
四用途投影、模型请求和结果保存上限；超过可接受大小时不选配相应工具。limit 不限制上游负荷。
分组摘要与最多100条组内明细分别投影，用本次响应内的 group_index 关联；明细放不下仍保留
可容纳的摘要并标截断。has_more=false 不保证投影完整；跨页不是一致快照。

结果保留抑制类别、静默 ID/匹配器/起止时间，不保存原配置、通知目的地、peer 地址或作者。
静默存在不证明命中告警，按 silencedBy 的 ID 关联；接收器名不证明通知已发送，过期记录不证明
当前仍被静默。uptime 表示启动时间，采集时间由小维另列。
401/403、5xx、网络不可达或超时按现有契约使该源本轮未核实，其他获准源可继续；本源本轮不再读。
未知状态（含400/404/422）、协议不合规、超限或证据失败仍中止，不自动修正 matcher 或重放。

`config check` 后停止旧进程、更新配置并重启生效。`/readyz` 的 `alertmanager.alertmanager-prod`
只表示 configured（已装配、未实读）或最后一次读取的 available/unavailable，不做探测，不影响
Web/StarRocks 就绪。源离线时历史与重发仍按本地当前授权复核；撤权则拒绝，不重跑 API。
回退到旧镜像前移除 `alertmanager_sources` 及相关工具授权，再用旧镜像 `config check`。
完整接口与验收只维护在[计划 §3.3](../docs/superpowers/plans/2026-10-09-monitoring-mcp.md#33-a4-当前切片基线-8a7072d86abf6e93e0fb5e4be8e84f61308feb80)；
真实模型、公司 Alertmanager 版本/容量/认证与真实飞书未验收。创建和取消静默本片均未开放。

## 状态、停止与重启

```sh
cd /opt/xiaowei/current
docker compose --env-file .env ps
docker compose --env-file .env logs --tail 200 xiaowei
docker compose --env-file .env stop xiaowei
docker compose --env-file .env down
```

`down` 不加 `-v`；命名卷会保留，但卷不是备份。小维使用有限的 `on-failure:3`，配置或数据库
错误不会无限重启刷日志。PostgreSQL 使用 `unless-stopped`。宿主机或 Docker 重启后统一执行：

```sh
cd /opt/xiaowei/current
docker compose --env-file .env up -d
docker compose --env-file .env ps
```

然后打开 `http://服务器IP:8501/readyz` 检查。本机 P3-B 只验证了停止小维服务后用指定服务名执行
`up -d xiaowei`；全栈停止后执行上面的 `up -d`、真实宿主机或 Docker 重启仍须在 P3-C 实测。

## 常用上限与函数名单

主模板按实际使用调过以下上限。它们不放宽只读账号、对象权限、证据复核或公司集群的内存限额，
只避免正常问题因为预算或期限过小而失败。已部署的配置不会随升级改变，需要时按此表手动调整，
不要用新模板覆盖自己的配置。

| 字段 | 模板值 | 过小时的现象 |
| --- | --- | --- |
| `model.request_timeout_seconds` | 120 | 模型思考较久时单次请求超时 |
| `model.max_output_tokens` | 16384 | Gemini 3 的思考也计入输出额度，过小会截断回答 |
| `data_policy.input.max_bytes` | 16000 | 贴一段长 SQL 或问题就被拒绝 |
| `budget.max_turns` / `max_tool_calls` | 16 / 16 | 列库翻页、查结构再查数时预算用完 |
| `budget.timeout_seconds` | 300 | 多步问题整轮超时 |
| `budget.max_scope_checks` | 2048 | 须不少于最大 `policy.max_rows` 的 5 倍 |
| `starrocks.query_timeout_seconds` / `client_timeout_seconds` | 60 / 65 | 大表查询超时；客户端期限不能早于服务端 |
| `policy.max_sql_bytes` | 16000 | 较长 SQL 被拒绝 |
| `schema_limits` 三个期限 | 300 / 900 / 120 | 日志出现“结构快照刷新失败 reason=timeout” |
| `audit.max_window_minutes` | 10080（7 天） | 更早的审计记录不在可查询窗口内 |
| `audit.candidate_bytes` | 8388608（8 MiB） | 一次候选读取达到字节上限时结果截断；调大也会增加内存占用 |

`query_mem_limit_bytes`、`max_rows`、`max_value_bytes` 保持原值：前者保护公司集群，后两者控制单次
回答的体量。

同一目标的后台结构刷新由 `SchemaCache` 合并为一次；对象、列、ID 读取和权限探测串行进行，
任何时刻最多占用该目标的 1 个连接槽位。刷新与前台查询共用 `pool_size` 个槽位，按进入
信号量等待队列的顺序竞争，没有为前台预留槽位；等槽位超过 `connect_timeout_seconds`
且本次工具调用尚未发出 SQL 时，前台会收到固定的“系统繁忙，可稍后再试”。

**函数名单按解析后的内部名填写。** SQLGuard 先把 SQL 解析再检查函数名，部分常用写法会被归一：

| 写法 | 名单中需要的名字 |
| --- | --- |
| `IFNULL(...)` | `COALESCE` |
| `SUBSTR(...)` | `SUBSTRING` |
| `DATE(...)`、`YEAR/MONTH/DAY/WEEK(...)` | `TS_OR_DS_TO_DATE`，以及对应的 `YEAR` 等 |
| `DATE_FORMAT(...)` | `TIME_TO_STR`、`TS_OR_DS_TO_TIMESTAMP` |
| `CURDATE()` | `CURRENT_DATE` |
| `FROM_UNIXTIME(...)` | `UNIX_TO_TIME` |
| `DATE_ADD(..., INTERVAL n unit)`、`DATE_SUB(..., INTERVAL n unit)` | `DATE_ADD`、`DATE_SUB` |
| `DATEDIFF(a, b)` | `DATEDIFF`（规范化后仍为 `DATEDIFF`） |
| `DATE_TRUNC('day', a)` | `TIMESTAMP_TRUNC` |
| `APPROX_COUNT_DISTINCT(...)` | `APPROX_DISTINCT` |

只写成平时习惯的名字（如 `IFNULL`、`DATE_FORMAT`）不会放行。模板名单经测试覆盖常用聚合、数值、
条件、日期、字符串和窗口函数。日期加减只接受整数常量区间，单位限 `SECOND`、`MINUTE`、
`HOUR`、`DAY`、`WEEK`、`MONTH`、`YEAR`；动态区间、未知单位和子查询在 I/O 前拒绝。
`DATE_ADD`、`DATE_SUB`、`ADDDATE`、`SUBDATE`、`DATE_TRUNC`、`DATEDIFF` 都只接受两个
顶层实参，缺少或多出实参会在执行前拒绝，不会静默丢弃。
`DATE_DIFF` 不因允许 `DATEDIFF` 而自动放行；两种写法须分别验证其语义。

## 普通升级与回退（schema 不变）

**升级到 Web 实战查询修复版：** 先按[常用上限与函数名单](#常用上限与函数名单)核对预算与名单，再在自己的配置中给需要列库的身份显式添加
`local/list_databases`：加入 `data_policy.model_tools` 和该身份的 `access.grants`；指定群使用时
还要加入 `feishu.group.tools`。不需要该能力的身份维持原授权；不要用新模板覆盖自己的配置。
内部表原始 DDL 为另一项独立授权 `local/show_create_table`：按需加入 Web 使用者的
`data_policy.model_tools` 和 `access.grants`；飞书群示例没有自动加入这项授权。它会返回内部表
默认值和完整表属性，接收范围应与使用者权限一致。目标的 `starrocks.max_ddl_bytes` 仅控制 DDL
单值容量（JSON 编码字节），未配置时沿用 `max_value_bytes`；示例为 64000，普通单值仍为 4000。
它不能大于 `max_result_bytes`，模型、Session、Web、飞书四种投影也须装得下配置的最大结果。
容量不足时整条 DDL 不返回并说明原因；按需调好容量、新建会话后重新读取，不重放旧结果。
显式日分区较多的表可能超过示例上限；升级前用目标集群的代表性 SHOW CREATE 原文测量 JSON 编码
字节数，再设定 DDL、结果及投影容量。历史 DDL 只保证采集时原文；当前复核要求采集时的列仍存在且
类型相同，不因新增列、属性或分区变化而失效，旧原文仍带采集时间显示；要看现状需重新查询。
本版字段类型由 `DATA_TYPE` 改为完整 `COLUMN_TYPE`，列元数据会变长；升级前按目标集群实际规模
核对 `schema_limits.max_bytes`（每条元数据读取的字节上限）和 `refresh_timeout_seconds`。
新快照超限时旧快照只保留到 `max_age_seconds`，到期后该目标的数据工具不可用。
存算分离集群的内部表可能报告 `CLOUD_NATIVE`；当前代码仅接受 `OLAP` / `CLOUD_NATIVE` 的
`BASE TABLE`，不放行外表。此分支只用离线契约验证了该路径，仍需在目标版本和账号上实测。
Web 有事实响应同时携带 `content`、结构化行与可展开的 `result_json`，维持现有 API；
未截短的值保留类型，截短值以带标记的字符串表示，无法从展示恢复原值或原类型。
用 64 KiB 合成 DDL 测得响应体约 193 KiB，约为原始结果 JSON 的 3 倍；这是有界容量开销，
不是公司负载的测量值。接入代理与浏览器的响应容量应按获准最大结果核对。页面只渲染一次事实表格。
PR B 使用 StarRocks 证据范围摘要 v7：普通查询的超长单值现在以有界前缀和
`…（原值已截短）` 标记显示，保留该行与后续行；总字节上限仍可能截断后续行。
内部表 DDL、结构快照和审计原文仍要求完整值。v6 及更早的 StarRocks 证据不能继续交付，
升级后新建会话再查。
此摘要与 PostgreSQL 应用表版本 6 无关，本次不新增存储迁移。

先把新包展开到 `/opt/xiaowei/releases/<新 SHA>`。查看当前和新版 `release.json`，并从当前
PostgreSQL 读取 schema：

```sh
cd /opt/xiaowei/current
cat release.json
cat /opt/xiaowei/releases/<新 SHA>/release.json
docker compose --env-file .env exec -T postgres sh -c \
  'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SELECT version FROM xiaowei_schema_version"'
```

升级不改 `.env` 里已有的 `XW_WEB_BIND_ADDRESS`：旧 `.env` 没有这一行时新版发布到 `0.0.0.0`；
从 `51622b1` 的模板复制来、写着 `127.0.0.1` 的仍只本机可访问，要让内网电脑直接打开就改成
`0.0.0.0`（或服务器内网 IP），旧版要求的 `web.allowed_origins` 可以保留。

只有数据库版本列在新版 `direct_start_schema_versions` 时才走普通升级。先留存旧控制文件和当前
使用的 JSON 配置（JSON 含单聊用户的 open_id，只允许部署管理员读取），再预检新版（新版镜像须已按
“获取 amd64 发行包”导入）；这些命令
失败不会停止旧服务。下面每个命令块里的 `config_file` 必须与 `.env` 的 `XW_CONFIG_FILE` 相同
（默认 `./xiaowei.json`；改过路径时按实际值填写，例如 `./config/prod.json`），每个块单独复制执行
时都要先确认这一行：

```sh
cd /opt/xiaowei/current
config_file='./xiaowei.json'  # 与 .env 的 XW_CONFIG_FILE 相同
previous=/opt/xiaowei/releases/<旧 SHA>
mkdir -p "$previous"
cp compose.yaml release.json "$previous"/
install -m 600 "$config_file" "$previous/xiaowei.json"
```

```sh
cd /opt/xiaowei/current
cp /opt/xiaowei/releases/<新 SHA>/compose.yaml compose.next.yaml
cp /opt/xiaowei/releases/<新 SHA>/release.json release.next.json
docker compose --env-file .env -f compose.next.yaml config --quiet
docker compose --env-file .env -f compose.next.yaml run --rm --no-deps xiaowei config check
docker compose --env-file .env -f compose.next.yaml run --rm --no-deps xiaowei model check
```

`model check` 会产生一次模型调用；模型配置没有变化且不想产生用量时可以省略。预检失败时按提示
修改 `XW_CONFIG_FILE` 指向的 JSON 后重新预检，旧服务继续运行（它只在启动时读取
配置）。预检通过后只重建小维；PostgreSQL 容器和命名卷不重建：

```sh
cd /opt/xiaowei/current
mv compose.next.yaml compose.yaml
mv release.next.json release.json
docker compose --env-file .env up -d --no-deps --force-recreate xiaowei --wait
docker compose --env-file .env ps
```

如果新版启动失败或需要回退，先用当前（候选）Compose 停止小维，停止成功后才恢复旧控制文件（含旧
镜像引用）和升级前的 JSON 配置，再按旧镜像预检并启动；停止失败时命令链中止，不覆盖任何文件。停止后
到旧版就绪前服务不可用。新版接受的配置旧版不一定能读，例如旧版要求 `feishu.users` 至少一人，
新版允许 `{}`；升级后在 JSON 里做的修改会随回退撤销，需要时按旧版规则重新填写。`.env`、CA 与数据库
不改（新增的 `.env` 项都是可选的，不需要补填；不要用新包模板覆盖 `.env`）。例外：回退到只接受
loopback/RFC1918 发布地址的旧版（如 `51622b1`）前，如果升级后把 `XW_WEB_BIND_ADDRESS` 改成了
`0.0.0.0` 或其他地址，先改回旧版接受的值，旧版的 `config check` 才能通过：

```sh
cd /opt/xiaowei/current
config_file='./xiaowei.json'  # 与 .env 的 XW_CONFIG_FILE 相同
previous=/opt/xiaowei/releases/<旧 SHA>
docker compose --env-file .env stop xiaowei &&
  cp "$previous/compose.yaml" compose.yaml &&
  cp "$previous/release.json" release.json &&
  cp "$previous/xiaowei.json" "$config_file" &&
  docker compose --env-file .env run --rm --no-deps xiaowei config check &&
  docker compose --env-file .env up -d --no-deps --force-recreate xiaowei --wait
```

`cp` 覆盖已有文件时保留实际配置文件原有的权限，容器 UID 65532 仍可读取；留存副本保持 600。

不要在新版验收前删除旧发行目录、旧镜像或执行镜像清理。普通升级不运行 `storage upgrade`。

## 成对备份

数据库与当时 `.env` 中的 `XW_DIGEST_KEY` 必须成对保存。数据库只保存不可逆指纹；换密钥会在
业务 I/O 前拒绝运行。下面先写临时文件，`pg_dump` 成功后才原子改名，失败不会留下貌似有效的
归档：

```sh
cd /opt/xiaowei/current
pair=/opt/xiaowei/backups/<日期时间>
install -d -m 700 "$pair"
cp -p .env "$pair/xiaowei.env"
tmp="$pair/.xiaowei.dump.tmp"
if docker compose --env-file .env exec -T postgres sh -c \
  'exec pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >"$tmp"; then
  mv "$tmp" "$pair/xiaowei.dump"
else
  rm -f "$tmp"
  exit 1
fi
test -s "$pair/xiaowei.dump"
```

备份会同时包含会话历史表和 `xiaowei_*` 应用表。把备份复制到其他受控存储；仅保留本机卷
不能抵御主机或卷损坏。

## 恢复到隔离数据库

不要覆盖当前数据库。先创建新库并用 `--exit-on-error` 恢复：

```sh
cd /opt/xiaowei/current
pair=/opt/xiaowei/backups/<日期时间>
docker compose --env-file .env exec -T postgres sh -c \
  'exec createdb -U "$POSTGRES_USER" -O "$POSTGRES_USER" xiaowei_restore_20261006'
docker compose --env-file .env exec -T postgres sh -c \
  'exec pg_restore --exit-on-error --no-owner -U "$POSTGRES_USER" \
  -d xiaowei_restore_20261006' <"$pair/xiaowei.dump"
```

Compose 中小维固定从当前目录的 `.env` 读取应用环境；命令行的 `--env-file` 只控制 Compose
变量插值，不能用另一个文件替换小维的应用环境。先停止小维，把当前引用保存为
`.env.before-restore`，再用配套备份的环境文件替换 `.env`。只把新 `.env` 中
`XW_DATABASE_URL` 的数据库名改成新库；保留其中原有的 `XW_DIGEST_KEY`，不要从模板重建：

```sh
cd /opt/xiaowei/current
pair=/opt/xiaowei/backups/<日期时间>
docker compose --env-file .env stop xiaowei
cp -p .env .env.before-restore
cp -p "$pair/xiaowei.env" .env
# 用安全编辑器只把 .env 的数据库名改为 xiaowei_restore_20261006
docker compose --env-file .env config --quiet
docker compose --env-file .env run --rm --no-deps xiaowei config check
docker compose --env-file .env up -d --no-deps --force-recreate xiaowei --wait
```

检查 `/readyz`、历史、请求编号和受控查询，并确认服务实际连接的新数据库；当前 `.env` 就是恢复
后的正式引用。若配置检查、启动或验收失败，保留两个数据库并恢复原引用：

```sh
cd /opt/xiaowei/current
cp -p .env.before-restore .env
docker compose --env-file .env config --quiet
docker compose --env-file .env run --rm --no-deps xiaowei config check
docker compose --env-file .env up -d --no-deps --force-recreate xiaowei --wait
```

不要清空卷、重新生成密钥或自动重跑未完成请求。恢复点之后的新记录不会出现在备份中；验收完成
前保留 `.env.before-restore`，并继续限制为仅部署管理员可读。

## 含迁移的升级

新版 `release.json` 不允许当前 schema 直接启动、但把它列在 `upgrade_schema_versions` 时，先按
上一节完成成对备份。确认当前 `.env` 仍含原部署的摘要密钥后：

```sh
cd /opt/xiaowei/current
docker compose --env-file .env stop xiaowei
# 按普通升级步骤保存旧控制文件，并把已预检的新 compose/release 切为当前文件
docker compose --env-file .env run --rm --no-deps xiaowei \
  storage upgrade --bind-existing-digest-key
docker compose --env-file .env up -d --no-deps --force-recreate xiaowei --wait
```

`--bind-existing-digest-key` 只表示操作者确认这是原密钥，程序无法从旧库证明。来源不明时不要执行。
迁移在一个事务内完成；普通 `serve` 遇到旧、未知或更高 schema 会以 1 退出，不会自动迁移。

迁移提交后若必须回到不认识新 schema 的旧程序，不能把原库原地降级：按“恢复到隔离数据库”把
迁移前备份恢复成新库，恢复旧 `compose.yaml`/`release.json`，并让旧程序使用匹配的旧配置与新库。
保留升级后的数据库，直到问题定位完成。

## 清理与请求排障

```sh
cd /opt/xiaowei/current
docker compose --env-file .env run --rm --no-deps xiaowei storage cleanup --batch-size 100
docker compose --env-file .env logs --tail 200 xiaowei
docker compose --env-file .env run --rm --no-deps xiaowei requests resend \
  --subject <subject> --chat <chat_id> --message <message_id>
docker compose --env-file .env run --rm --no-deps xiaowei requests resend \
  --subject <发起人 open_id> --group --message <message_id>
```

重发只处理已保存的 failed/unknown 结果，重新检查当前权限和群成员资格，不重跑模型或原业务
SQL。未完成请求在启动恢复时记为中断，同样不自动重跑。日志只记录安全状态、原因码和请求编号；
Compose 的 `XW_LOG_MAX_SIZE`/`XW_LOG_MAX_FILES` 限制本地日志轮转。Web 实战修复版接受请求时记录
`channel=web request="页面编号" turn=内部编号`；先按页面编号找到 turn，再看该 turn 的
`stage=failed reason=...`（例如 `timeout`、`turn_limit`、`answer_rejected`、`tool_failed` 或
`model_failed`）。日志不含问题正文或模型错误原文；旧版本没有该关联行时，不能从通用回执猜原因。
`model_failed` 还会记录固定分类 `error_kind` 和可用的 `http_status`：`provider_http` 是模型服务
HTTP 错误，`connection`/`model_timeout` 是连接/模型调用超时，`invalid_output` 是模型返回不符合
最终输出契约，`request_rejected`/`response_rejected` 是模型请求/响应边界拒绝，`model_refusal` 是模型
拒答；运行配置错误另有固定分类，`unexpected_error` 仍是未分类异常，不能据此认定
模型服务故障。状态码缺失记 `-`。这些分类只改善排查，不引入自动重试，也不记录异常正文。

PostgreSQL 16 同一大版本内更换镜像 digest 不在本轮验证范围，须另做备份、兼容和回退演练。
