# 小维容器运维

本说明适用于发行包里的 `compose.yaml` 与 `release.json`。部署只有小维和 PostgreSQL 两个容器；
Web 端口默认对服务器所有网卡开放，内网电脑用 `http://服务器IP:8501` 直接访问；PostgreSQL 不发布
端口。**Web 没有登录：能访问这个端口的人都以 `web.operator_id` 的身份和权限查询。** 访问范围由
服务器网络和防火墙决定；只想本机访问时在 `.env` 设 `XW_WEB_BIND_ADDRESS=127.0.0.1`。

建议使用固定目录 `/opt/xiaowei/current`。`.env`、`xiaowei.json` 与 `certs/` 由操作者持有，升级
只替换 `compose.yaml` 和 `release.json`；升级前留存的旧 JSON 配置（`.env` 的 `XW_CONFIG_FILE`
指向的文件，默认 `./xiaowei.json`）用于回退。旧发行包及旧应用镜像保留到新版验收完成。

## 获取 amd64 发行包

发行包是一个文件 `xiaowei-<完整代码 SHA>-linux-amd64.tar.gz`，向发布方索取。发布时可填版本号（如 `V0.1`），
只用于发行标题（“Xiaowei linux/amd64 V0.1”）和说明中的 `Version:`；文件名、镜像 `xiaowei:<代码 SHA>`
与下面的命令仍按代码 SHA，从发行说明的 `Source commit:` 取得。它已经带着应用镜像
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
| `description` | 这个集群的用途说明，帮助模型识别环境；会原样交给模型，不代表默认或已选择，不要写“默认用此集群”“可跨环境查找”之类的话 |
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

### 历史简短追问与 R3 兼容

R3 只改变事实显示和提示词，不改配置、数据库 schema、权限、查询预算或 Session 保留上限。
“刚才本次返回多少条”可引用已有可见历史事实，以摘要显示本次返回行数；COUNT 等聚合值不等于
结果行数。来源、时间、固定说明、分页与丢失提示仍可见。要原文、字段值或最新结果请明确提出；
新查询结果仍默认完整展示。显示摘要不会压缩历史，也不延长会话。

Web 折叠保留当前获准原表与 JSON；飞书在原容量内折叠保留获准 JSON。容量不足时卡片明确显示
“获准原文未展示”，不能从摘要推断整份原文已经发送。原文或容量说明成功发送后仍为
`sent`，操作者的 `requests resend` 不会为已发送消息补发原文。分页不代表原始值完整，
已有 Web 分页标记继续显示为“结果已截断”。

新模型输出增加 `fact_views`（只允许 `full/brief`，未指定引用按 `full`）；新代码能解析旧回答。
`extra="forbid"` 不放宽，所以旧镜像不能历史读取或重发带此字段的新保存回答。如需回退，
优先保留新字段解析并恢复完整显示；直接回到 R3 之前的镜像时需新建会话，相关新格式旧结果须
在恢复支持该字段的镜像后读取或重发，不删除保存记录。

因本地受公司模型 API IP 限制，用户 2026-10-11 批准 R3 免做真实模型旧/新对照：
真实模型未验证，上线后先在私聊或测试群观察。样例脚本保留；该例外仅适用于 R3，
独立复审及 CI 门槛保留。真实 Lark 客户端与收发仍未验收；合并、部署须用户另行授权。

### 飞书未支持类型回执

E5a 只增加图片、文件、富文本 post 等非文本的固定说明，未启用附件下载、读取或识图；无需新配置、迁移或额外权限。通过应用/租户、发送者、会话、时效和消息标识检查后，已登记单聊仍复核当前授权；未登记有效单聊沿身份提示。指定群必须在平台 mentions 中 @本机器人，且发送前可确认当前成员；非指定群、未 @、过期/非法事件和无法确认身份均不回应。post 中的 at 标签不算平台 mention。

不支持类型与身份提示共用现有每 open_id 10 分钟一次的内存窗口，各索引至多 1024 项；同一消息仅尝试一次，发送失败/未知或取消不自动再发。跨私聊/指定群也共享该发送者窗口，重启或索引淘汰后可能再次提示。固定说明不创建业务请求、Session 或 Evidence，不占交互轮数；没有保存结果可供 `requests resend`，需改发普通文字。自动媒体拉取、转发/卡片展开和姓名查询继续关闭。

这些是入口协议与离线证据，真实 Lark 收发和群内纯图片 mention 尚未验收。不能先发“@小维”再发另一张图来配对，本片没有跨消息接续。回退可单独撤回 E5a 入口变化，重新表现为非文本静默丢弃；v8 数据库与已有会话/回答格式不变，整镜像回退仍遵守版本兼容说明。部署和真实环境观察需另获授权。

### 飞书 TXT/MD 输入

`feishu.document_input_enabled` 默认 `false`，保留 E5a 的固定提示。启用时在飞书段写
`"document_input_enabled": true`，运行 `config check` 并重启应用；只读取平台原消息资源中的
UTF-8 `.txt`/`.md` 文件。按文件下载的原始字节最多为 `4 × max_message_chars`（配置允许范围内
至多 40,000 字节），实际正文仍须同时满足原 `max_message_chars` 和模型 Profile 的输入字节及
禁止模式策略；不会自动截短或提升现有文字限额。超限、编码错误或下载失败只发一次固定说明，
不会创建业务轮次。文件名和资源键不交给模型；受控下载只用当前配置对应的官方域名和原消息
标识，拒绝非 identity 编码、重定向与超限正文，总期限 30 秒。TXT/MD 内容作为不可信资料进入
诊断用途，不把文件里的 `/新建`、`/批准` 当成入口命令；执行权限仍由现有治理逐次复核。

这项只有本机合成 Lark 端点、真实 Agent 运行器和隔离 PostgreSQL 的离线证据；真实 Lark 收发与
真实模型阅读/抗注入能力未验收。PNG/JPEG 输入及原图回放须按下一节单独开启。旧镜像的配置
`extra="forbid"` 会拒绝新字段，回退时先删除 `document_input_enabled` 并用旧镜像运行
`config check`，再按版本兼容步骤启动；旧镜像不能处理新文件消息，已保存的文本请求按原 Session
与投递规则读取。部署与私聊/测试群观察须另获用户授权。

### 飞书 PNG/JPEG 输入

本片为离线候选，真实 Vertex 识图和 Lark 收发尚未验收。图片默认关闭；启用前须先完成获准模型的固定业务样例和真实平台私聊或测试群验收，并另获部署授权。仅在飞书配置写入 `"image_input_enabled": true`，同时在 Vertex 模型 Profile 写入同名字段；其他模型 Provider 不支持该开关。`document_input_enabled` 可独立配置。旧配置不变，文字消息与 TXT/MD 限额不变。

单条图片消息只接受原值不超过 2 MiB 的 PNG/JPEG，完整解码后每边最多 8192 像素、总像素最多 16,777,216；不缩放、压缩或截断。一个会话最多保留 12 张有效原图，每轮回放全部原图。容量已满会在下载前给固定回执；下载、格式、期限或权限无法核实时拒绝本轮，不以模型摘要代替原图。群图片仍须在同一条消息有平台可信 @本机器人且成员当前有权，不能先 @ 再另发图片。存储在 PostgreSQL 应用表 v9，按会话到期清理；最坏每活动会话约 24 MiB，备份及数据库容量需按活动会话数估算。原图不会进入对话文字历史或 Evidence。模型看到图片后作出的识别结论仍是推断。

v8 数据库不能直接用 v9 镜像启动；按[含迁移的升级](#含迁移的升级)先做成对备份、停止旧进程，再显式运行 `storage upgrade`。旧镜像的配置校验会拒绝 `image_input_enabled`，回退时先从飞书及模型段删除该字段。v9 没有原地降级：回退到不认识 v9 的旧镜像还须恢复迁移前的隔离数据库与配套配置，不能只切换镜像或修改版本号。已上传的图片只在 v9 会话内有效，恢复到迁移前数据库后不保留。

图片下载在事件回调中最多占用 30 秒，多个同时变慢的附件会占用入站名额；首次启用先在私聊或测试群限制并发上传，并观察 `intake_full`。若文字消息也被挤掉，应关闭飞书图片输入开关并排查，不把这次丢失当作平台重投；此开关只停止新图片接收，已有会话仍可在模型图片开关保持开启时回放原图。要同时停止回放，须关闭模型图片开关并新建会话，旧会话的 Profile 绑定会拒绝继续。上线后还要逐项观察：图片上传与后续文字追问是否读到同一原图、达到 12 张后是否明确拒绝、群 @ 与成员撤权是否生效、12 图请求是否被真实 Vertex 接受、模型是否把图片内容误当指令。已获准模型样例和真实 Lark 事件未验证前，不把离线替身结果记为通过。

### 飞书结果展示与容量

普通解释、问候、建议和澄清优先使用富文本；小结果使用 JSON 2.0 卡片，回答在结果前，每条分析标明对应结果序号，分析与结果之间有独立分隔线；技术来源在“查看依据”。业务范围、分页/截断、未核实来源及未分类限制保持主展示；看板摘要明确是配置定义。审批卡保持独立路径和完整材料。

受控 Markdown 只启用标题（加粗）、`**` 加粗、列表和换行；双下划线标识符和其他语法保持字面文字。每个消息部分保持单段；平台明确拒绝富文本格式时，可降级为文字，仍须有消息 ID 才算送达，不保证保留原排版。代码、嵌套列表、反斜杠、末尾空白、可能触发 @ 的文字或需折叠依据的说明继续用原文卡片。结果不明时不会自动换内容重发，恢复方式见下节。

事实原值、实际 SQL/DDL、系统未核实说明不参与模型格式解析。真正的 NULL 显示为 `NULL（空值）`，字符串 `"NULL"`、空串与未提供字段分别显示；换行保留，不可见控制符显示为转义。结构/DDL 是结果时默认显示，未知限制不能因排版被隐藏。普通短回复没有附件；新语义请求的完整消息放不下时转完整文件加正文，详见[飞书报告文件](#飞书报告文件)。

旧语义请求的预览容量仍按“必要说明 → 实际 SQL → 整行事实 → 分析 → 其他详情”分配；新语义请求放不下时改用完整文件。

本次展示改动不需改配置或迁移数据，也不改变读取权限；已绑定的旧消息/报告补发原字节，不要求当前模板重建。展示回退不等于旧镜像可读新表，镜像与表结构回退仍按对应版本恢复流程。

**客户端前提：** [官方 JSON 2.0 文档](https://open.feishu.cn/document/feishu-cards/card-json-v2-structure)
要求飞书/Lark 客户端至少 7.20，较旧客户端可能只显示标题。上线前由操作者确认桌面和移动端版本，
真实客户端展示仍需验收。不做版本探测；结果不明时不自动重发，明确格式拒绝的文字降级按上述规则处理。
下列真实验收全部未验证，另获部署与环境授权后先限私聊或测试群：

1. 优先检查 ASCII 标点数字实体是否正确解码（尤其 `.` 与双下划线），不得显示满屏实体；没有客户端证据前不收窄转义范围。
2. 桌面/手机的加粗、标题、两类列表、两空格嵌套列表和换行。
3. 7.20 以下旧客户端的实际表现。
4. 纯文字回退中代码和 SQL 草稿复制所得原文。
5. 实际收发、操作者重发及容量边界能否被平台接受。
6. 真实模型在计划固定任务集中的工具选择、原文引用、限制表达和展示效果；本片未改模型指令，任务与展示验收仍待真实证据。

新请求的结果卡片与 post 遵守完整 HTTP 请求体 UTF-8 的 **30,000 字节**和每卡 200 组件；`max_reply_chars`（200–3500）只约束纯文本消息（固定回执、会话提示）及渠道组件的分段上限。旧 v0 请求与审批材料卡仍同时遵守 `max_reply_chars`，其字符包括可见及折叠文字；卡片的格式符/实体保守计入，post 按转换后的 text 节点计数，字节按内层 JSON 字符串和外层再次转义实测。任一上限不合就保留完整的较简格式，或通过 D1a/D1b 改用文件，不依赖 渠道组件 隐式分段。折叠不会节省容量，已保存分析同样须完整交付。旧 v0 请求继续兼容旧容量行为，旧 sent 不追认完整。

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

### 飞书报告文件

现有分项/分卷交付已合入；模型自主 HTML/MD 正文为本地候选，未部署或真实验收。完整交付范围、容量和权限见 [ARCHITECTURE §7](../ARCHITECTURE.md#result-delivery)，切片验收见[报告计划](../docs/superpowers/plans/2026-10-19-model-authored-reports.md)。普通短结果仍为单条完整消息；必要或明确请求的文件逐份上传并确认文件消息，全部确认后才发正文。每个文件不超过 400,000 字节；超出时按整行或章节拆成 `1/N`，单个模型文字块不可拆。原查询行数/单值限额不变；监控曲线未实现。

显式 MD 使用静态 Markdown；类型名中的尖括号、原始 HTML、图片、链接或引用定义若被解析为活动构造，程序保留原文，将所在段落/列表/表格改为字面代码段并附“部分不支持的格式已按原文展示”说明，其余块继续原生排版。不会加载图片或激活 HTML/链接。代码围栏应显式闭合，列表/引用内未闭合时明确未完成，避免吞入系统事实。显式 HTML 使用独立片段和受限内联 CSS，HTML/SQL 示例及 `ARRAY<INT>` 等类型名须转义；仅套 code 标签仍须转义尖括号。字号为 12–64px/0.75–4rem、完全透明的前景色不保留，间距上限 128px/8rem、宽度上限 1280px/100%，配合窄屏外壳约束；这些是当前设计样例下的工程取值，不是通用安全标准。HTML 危险行为/格式被移除但正文完整时附“部分不支持的格式已移除”提示；无可读正文或不能保真交付时提示未完整，不重跑查询。预览只提取文字，系统事实仍从获准 Evidence 生成。旧候选因上述常见 MD 构造失败且尚未绑定文件的版本 3 回答，可在本修复后用原请求补发，不重查；已经绑定的文件仍复用原字节。仍无法安全处理的正文只提示简化后重新提出，不建议无效补发。

新应用在既有 JSON 中写入可信回答版本 3，模型字段与数据库 schema v12 不变；旧版本 2/Legacy 仍按文字渲染，包括升级时已保存但尚未绑定附件的请求。已绑定附件/消息复用原字节和键。回退到不支持回答版本 3 的程序不能读取或重发这些新回答；不要删记录、改版本号或以旧程序重新生成文件规避。常规回退必须保留数据并先核对应用回答版本兼容性。

模型输出上限和单文件容量不是同一个限制。上游输出截断仍返回通用模型失败回执，不自动续写或改写。操作员可查固定日志 `reject_reason=finish_reason_max_tokens`（Vertex）或对应协议的不完整终态，确认后建议缩短报告或拆分请求；不要因通用失败文案就断言一定是输出超限，也不需开启真实数据 tracing。

应用表升至v12：迁移012只扩展父请求语义版本和最小分项表，分项字节/资源键随原请求级联清理；已有v11报告与回执不改写，旧sent不晋级为新完整语义。升级按[含迁移的升级](#含迁移的升级)先备份并停止旧程序，再运行`storage upgrade`；普通serve不会自动迁移。不修改现有配置，保持原密钥。回退到不支持v12的镜像必须恢复迁移前配套数据库/配置，不改版本号或原地降级；已被飞书接受的内容不会随数据库回退撤回。

生产审批卡首次和补发都独立先发，并检查材料自身容量，数据失败不阻挡其补发或已确认材料的有效批准。每次投递开始完整复核一次当前权限/Evidence，途中撤权窗口按已批准C8接受，下一次补发重查；各次I/O仍检查保留期、readiness及owner/attempt。操作员通过既有`requests resend`显式补发缺失部分，不重跑模型或查询、不自动重试；已确认部分不再发送，新部分使用首次保存的uuid和原字节。上传未知禁止重传；正文只有附件消息全部确认后才能补发。同一分项曾为发送结果不明时，后续明确失败仍不抹去此前的不明；新请求整体状态从所有分项推导，旧请求保留原保护。数据仍不合容量时CLI直接提示“容量不足，不能重发”，这些数据零外发，整轮仍未完整。

CLI补发前根据实际请求说明旧请求无稳定uuid或新请求分项保存/复用uuid，并提示可能重复；真实平台去重窗口和重复响应尚待验证，拿到平台消息ID前不能推断送达。结果不明时提示操作员可手动补发未知消息部分，上传结果不明仍禁止重传。生成/上传/消息失败时只尽力发送一次未完整提示，不把提示计为必需部分或自动补发。存储故障仍锁低readiness并按既有恢复流程处理。

本地替身证明调用链、协议和状态边界，不能证明真实Agent会正确选择工具、取证/修正或回答任务，也不能证明真实飞书去重与客户端保真。用户要求本地审核验收后才考虑PR与合并；真实模型、私聊/测试群及手机/桌面验收保留，当前未获部署授权。

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

### 选配经典 Dashboard 创建和修改

D7 默认不启用。只在指定群逐次批准；产品边界以[架构 D7 契约](../ARCHITECTURE.md#monitoring-mcp)为准。
本机已用官方 MCP v2.0.2 / 隔离 Grafana13.2.3 验证，不代表公司版本、真实群或模型已验收。
保留上面的只读端；在外部节点另开 Dashboard 写端，只开下面类别（本机已核对唯一写工具）：

```text
--transport=streamable-http --address=0.0.0.0:8001 --enabled-tools=search,dashboard
--disable-query --usage-stats=disabled
--allow-grafana-url-override=false --dynamic-multi-org=false
--server-auth-token=<从外部节点安全运行配置提供>
```

此写端不加 `--disable-write`；锁版的 `--enable-write-tools=update_dashboard` 不能覆盖这个开关。
写端的 Service Account 必须固定 Grafana URL/组织及已确认共享权限，调用者 Bearer 与上游凭据分别配置。
部署时重验无/错调用者凭据为401且零写入，正确凭据不能直调未选写工具（含 grafana_api_request）。
不要将数据源/Annotation 类别混入这个写端。

在现有 `mcp_servers` 追加一项，既有只读端不变：

```json
{
  "server_id": "grafana-write",
  "url": "http://grafana-mcp.internal:8001/mcp",
  "auth_ref": "env:XW_GRAFANA_WRITE_MCP_TOKEN",
  "timeout_seconds": 5,
  "max_response_bytes": 128000,
  "allowed_tools": {
    "get_dashboard_by_uid": "grafana.get_dashboard_by_uid",
    "list_dashboard_versions": "grafana.list_dashboard_versions",
    "update_dashboard": "grafana.update_dashboard"
  }
}
```

将 `grafana-write/propose_create_dashboard`、`grafana-write/propose_update_dashboard` 和
`grafana-write/get_action_status` 加入现有 `data_policy.model_tools` 与 `feishu.group.tools`；
群成员可申请，`access.grants` 中给有审批权的成员 `grafana-write/update_dashboard`，沿用真实 open_id。
实际写工具不加进模型/群共享工具表。需要完整定义辅助编辑的已授权用户/群可另外加入
`get_dashboard_by_uid`、`list_dashboard_versions` 的同源工具 ID；原生v2可读、不可提出写。
提出动作授权包含该动作固定的同源只读准备，不查询其他源，不执行面板实时查询。

设置能容纳完整材料的四用途 `projection_bytes`（本机用60000），并按实际 Dashboard 大小验收；
JSON定义上限16000字节，完整差异/恢复材料过大时不保存可批准 Action，不提高或截断来假装完整。
这不是所有 Dashboard 的容量保证；还受现有飞书3500字符单卡片/完整请求预算限制。
群显示完整before/after，恢复说明引用其中before；材料不完整送达时不能批准。
审批JSON用纯文本显示原内容，避免标量字符串二次转义；不会截断必要定义来获取批准。
版本由保存历史读取，不能从当前响应缺失字段中猜测；
读取版本0只是本地“当前”的约定，发送到上游时省略 version。更新不使用 patch。

批准后版本变化时未执行、需重新提出；原子保存竞争的锁版409也明确未执行。
其他写后错误/超时/回读不符显示结果未知，先按 Action ID 和远端 UID 核对，不再批准、不自动重放。
如需恢复旧定义，经另次申请/批准，以最新版本恢复；创建撤销由管理员按UID人工删除并记录。
普通取消/超时的实例恢复沿用下节，D7不新增迁移、后台任务或自动回滚。

启用前运行 config check；停止旧进程后切换配置。回退到旧镜像前移除写端及全部新增工具授权；
已成功/未知 Action 先人工核对，保留 PostgreSQL 备份，不重放写请求。
公司版本的冲突/回读、组织权限、provisioning限制、真实群展示和恢复实测仍为上线前门槛。

### 选配 Grafana 数据源与 Annotation 写端（C8）

C8 默认不启用，原 G3 只读端保持独立。外部节点另开经调用者认证的 Grafana MCP v2.0.2 写端；
小维不代管该 Server，也不要求新 CA。内网 HTTP 会明文传输 Bearer 与内容，端点和令牌只由可信配置提供：

```text
--transport=streamable-http --address=0.0.0.0:8002 --enabled-tools=datasource,annotations
--disable-query --usage-stats=disabled
--allow-grafana-url-override=false --dynamic-multi-org=false
--server-auth-token=<从外部节点安全运行配置提供>
```

此写端不能加 `--disable-write`，也不加写工具筛选参数。官方类别还会注册未选中的删除/修改
Annotation 等工具；小维只映射下面列出的创建/修改和必要回读。**持有写端令牌者可以绕过小维群审批，
直调远端全部工具（包括删除）**，这是已接受的风险；限制该端网络可达范围并保管令牌。
启用前验证不带或带错 Bearer 均为401、零上游写入；正确令牌只用于小维配置的固定端点。

```json
{
  "server_id": "grafana-c8",
  "url": "http://grafana-mcp.internal:8002/mcp",
  "auth_ref": "env:XW_GRAFANA_C8_MCP_TOKEN",
  "timeout_seconds": 5,
  "max_response_bytes": 128000,
  "allowed_tools": {
    "get_annotations": "grafana.get_annotations",
    "create_annotation": "grafana.create_annotation",
    "get_datasource": "grafana.get_datasource",
    "create_datasource": "grafana.create_datasource",
    "update_datasource": "grafana.update_datasource"
  }
}
```

可只选配 Annotation 或数据源；前者必须带 `get_annotations`，后者必须带 `get_datasource`。
在 `data_policy.model_tools` 与 `feishu.group.tools` 只加入获准的
`grafana-c8/propose_create_annotation`、`grafana-c8/propose_create_datasource`、
`grafana-c8/propose_update_datasource`、`grafana-c8/get_action_status`；审批名单成员的
`access.grants` 才加入对应 `grafana-c8/create_annotation`、`grafana-c8/create_datasource`、
`grafana-c8/update_datasource`。实际写工具、删除和通用 API 工具不进入模型工具表。
Web 和私聊不能申请或批准这些写动作。群内每个 Action 单独批准，版本变化未写入须重新提出，
写后超时或回读不符按结果未知处理、人工核对、绝不自动重放。

`settings_json` 用锁版 MCP 的实际字段，不按插件类型建白名单，也没有逐类验证或小维 URL
目标白名单。提出阶段只用 `create_datasource` 的空 `name` + `type`，或 `update_datasource`
的单独 `uid` 获取字段说明；这两个锁版分支已实测不写入。**不要**把非空 `name`、未知插件
和省略 `schemaReviewed` 的组合当作只读探测：它可能直接 POST。返回 `fields` 说明时，
`settings_json.fields` 只能使用说明列出的键及其基础值类型；不认识的键在提出时拒绝并返回可用键名，基础值类型不符也在提出时拒绝。
没有 `fields` 说明的插件须用顶层 `url` 等参数，提交 `fields` 会拒绝；`fields` 与
`jsonData` 也不能同时提交。只有群批准后的写调用才带 `schemaReviewed=true`。
插件默认值、字段语义、Grafana 可访问的目标地址仍由启用者按实际安装环境判断，
本机 Prometheus 插件成功不证明所有插件成功。`secureJsonData`、有值的 password/token/apiKey/secret
动态字段及带账号密码或已识别凭据查询参数的 URL 在提出阶段拒绝；需密码时群回执显示“待配置凭据”，
管理员到 Grafana 数据源设置页手动填写，健康检查前不称可用。修改 `jsonData` 时，
小维回读并合并现有非秘密字段后展示完整待写内容；现有字段被隐藏则拒绝局部修改，
由管理员在 Grafana 手动处理。未知插件若把密码放在不含上述关键词的普通字段里，
本地字段名检查无法识别，操作者不得把它交给群提案。已批准的飞书私聊
`/设置密码` 是**后续独立切片**，本版未开放；不要把密码发到群里。

启用前用目标 Grafana 版本检查响应大小、Service Account 组织权限、群成员与审批名单，
再做一次成功创建/修改/标记、一次未授权零写入及一次外部抢先修改的验收。
`config check` 后停止旧进程并切换配置。回退旧镜像前移除 C8 写端和新增授权；
已成功/未知的 Action 先按 UID/ID 在 Grafana 核实，保留 PostgreSQL 备份，不重放写入。

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
选择 HTTPS 时沿用正常证书验证。Basic 只验证调用者身份，不能证明账号仅可读；
未选配下列写动作时，小维只发固定 GET。

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
真实模型、公司 Alertmanager 版本/容量/认证与真实飞书未验收。

### 选配创建和取消静默

S6 在同一个 API v2 源上选配，默认 `write_actions` 为空，旧配置仍只读。
需要创建及提前结束时，在该源对象中增加：

```json
"write_actions": ["create_silence", "cancel_silence"]
```

两项可单独选择。写源必须成对配置 Basic 安全引用，不能用无认证配置开放写动作。
把 `alertmanager-prod/propose_create_silence`、`alertmanager-prod/propose_cancel_silence`
及 `alertmanager-prod/get_action_status` 按需加入已有 `data_policy.model_tools` 和
`feishu.group.tools`；把实际 `alertmanager-prod/create_silence`、
`alertmanager-prod/cancel_silence` 只加入审批成员的 `access.grants`。沿用下节群审批，
不新建审批名单，不把实际写工具加入模型或群共享工具列表。Web、单聊不提出或执行写动作。

群员要求静默指定主机或告警 → 小维展示准确匹配器、当前命中数量/最多5个标签与指纹样例、
绝对起止时间、用途及恢复办法 → 名单成员在群内逐次批准 → 创建后展示实际回读的 UUID 和时间。
匹配器的 name/value/isRegex/isEqual 均显式传入，正则按整串匹配；没有主机/指标模板白名单。
如果过滤响应超出接收上限，材料明确“数量未知”，不能将样例或部分响应当完整计数。
匹配数量和样例不预测将来命中；它们或对象内容在批准前变化时，需要重新提出。

支持1年、3年等明确到期的静默，不提供永久或自动续期。批准有效期仍是15分钟。
起止时间规范化为 UTC 毫秒；过去开始时间由官方服务移到执行时刻，不能声称此前已生效；
未来开始时间和结束时间须保持批准值，成功回执显示实际快照。
未来开始按发出创建请求前的本机时钟判定；若远端时钟较快而把它改成当前开始，即使静默已创建也会记为未知，
因为它未保持批准的未来开始值。先人工核对，不再次批准或自动补写；不新增钟差配置。
取消使用已知静默 UUID 另提动作并批准，无须固定先查列表；它使 active/pending 静默提前到期，
保留记录，已到期则不再提出。恢复需按原匹配器和明确期限另提创建，不能复活原 UUID。

创建静默 POST 返回400时，上游确定拒绝、动作记“未执行”，核对或修改匹配器后重新提出并再次批准；
正则由提出阶段的上游过滤查询校验，不增加本地正则库。至少一个匹配器不匹配空串；
纯否定匹配器以上游规则为准，不额外限制。到期、静默ID不存在和过滤400会返回具体原因，保留已有调查证据。
这项创建400分类不适用于取消、写后回读或其他状态。其余写后非200、超时、响应或回读不符，
状态为“结果未知”，先到 Alertmanager 人工核对；
重复批准、重启或下一轮不会自动重放。查询 Action 状态得到的是应用记录，不是远端实时状态。
官方 API 没有原子版本条件，写前回读不能消除最后一段竞争或 HA 传播延迟；
生产启用前须核对实际版本、Basic 拒绝、数据容量、HA 行为和此残余风险，实际通知恢复还受路由与时间影响。

本机只验收官方 v0.34.1、合成数据和群事件替身，真实模型与飞书写审批未验收。
完整接口与证据入口见[计划 §3.5](../docs/superpowers/plans/2026-10-09-monitoring-mcp.md#35-s6-当前切片基线-b0174d273bce89b49bfbba82fedf86bb6a14c66b)。
停旧进程后调整配置，`config check` 通过再启动；无需新服务、依赖或数据库迁移。
撤回写能力时移除 `write_actions`、两项提出/实际写工具授权，保留所需只读与状态工具；
退回 S6 前镜像时也须移除新配置字段。待审批/未知动作先核对，不自动补写；
仅回退 S6 不改变 v7 数据库，退回 W5 前版本仍按成对恢复流程处理。

## 监控群审批底座

W5 交付审批底座，S6 选配创建/取消静默；D7 可选配经典 Dashboard 创建/修改。群内调用选中动作的
“提出”工具后，代码生成实际参数、版本、差异、影响、恢复办法、Action ID 和批准截止时间。
模型需要执行时，以 Action ID 请求暂停并送达审批材料。有审批权的成员在原群
`@小维 /批准 <Action ID>`（或 `/拒绝 <Action ID>`），应用重新核对当前群成员、权限和材料，
再占用一次执行机会；申请人有审批权时可自己确认。等待批准期间不占群队列，批准事件
与调查在同群按收到顺序处理；Web 和单聊均不批准，不直接执行写工具。

每个已配置监控源登记本地 `<server_id>/get_action_status`：按需加入已有
`data_policy.model_tools` 和 `feishu.group.tools` 即可查询本群动作，不能靠配置开放未实现的写工具。
写切片通过 `access.grants` 中对应写工具的权限指定审批人（键为其群事件 `open_id`），
不增加第二份名单；实际写工具不进入模型工具集或群共享工具列表。

批准期限固定为15分钟，且不超过原请求/证据保留期；它限制本次批准有效期，不限制未来
静默的1年/3年时长。材料未完整送达（包括只发容量提示）、过期、对象版本/参数/源绑定
变化都不能执行，需重新提出。结果未知或处理中断时不自动重试或补跑；可用 Action ID
查询应用执行记录并人工核对远端。状态不是远端实时健康检查，pending 不保证可以批准。
执行记录独立于会话历史，关闭/清理群会话不会提前删除仍有效记录；恢复成功的原轮完整报告经 PolicySession 写入历史；恢复/模型/会话失败则独立反馈执行事实，
后续提问时小维可按需查状态，新建群会话后仍可查本群尚在保留期内的动作。

原生审批接续采用应用表 v10。原会话已封存、关闭或过期，以及发起人退群/历史证据权限变化时，
旧候选不恢复、不执行，回执说明原因及重新提出方法。候选过期或会话作废后，暂停前未完成的
报告不交付、不入历史；已存 Evidence 按原保留规则处理。只提出而未形成可恢复暂停态的 Action
不能独立批准；不从聊天历史猜造恢复状态，也没有旧审批执行后备。含原图的会话不提供执行请求，
其中的动作只能提出，需 `/新建` 会话后重新提出。

升级前按“含迁移的升级”停止旧进程并备份，显式 `storage upgrade` 升至 v10（迁移009原图表、010审批检查点）。
旧 pending 统一作废，旧 ID 批准固定回复“已因升级作废、未执行，请重新提出”；旧 succeeded/unknown
保留，executing 不在迁移中修改，启动恢复为 unknown。不要因升级或重启重新发写请求。
过期审批检查点由现有显式 `storage cleanup` 有界清理，无额外后台任务；暂停材料未发送时，
启动恢复记发送失败，操作者可按既有重发命令显式重发，送达前不能批准。

普通审批超时或局部取消、且关键记录已保存时，不需要重启小维，其他只读调查可继续。
回执显示“未执行”时核对条件后重新提出；显示“已执行、反馈未保存”时按 Action ID 查状态，
不要重复批准；显示“结果未知”时先人工核对远端，客户端取消不代表远端已停止。
若关键记录保存失败、收尾再次中断或实例失锁，`/readyz` 仍不就绪，需按原有停止/重启流程
恢复；重启不会补写动作。群消费者、队列和消息投递的中断保护保持原有规则。
具体执行边界见[架构的群审批约定](../ARCHITECTURE.md#monitoring-mcp)。

W5 的应用表版本是7，需要按下面“含迁移的升级”备份、停旧进程、显式迁移后启动。
离线合成入口验证不能证明真实飞书、真实模型或监控写接口可用。

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

审计表直查时，`timestamp` 按 `audit.time_zone`（FE 系统时区）写入，`NOW()` 按目标的会话 `time_zone` 计算；两者不同会让时间比较偏移，部署时须分别核对。时间条件只建议给大表查询，不是程序强制要求；无条件查询仍受已配置的超时和内存上限约束。`list_slow_queries` 与直接查询的口径不同，前者不列身份字段和审计表自身读取。

`query_mem_limit_bytes`、`max_rows`、`max_value_bytes` 保持原值：前者保护公司集群，后两者控制单次
回答的体量。

同一目标的后台结构刷新由 `SchemaCache` 合并为一次；对象、列、ID 读取和权限探测串行进行，
任何时刻最多占用该目标的 1 个连接槽位。刷新与前台查询共用 `pool_size` 个槽位，按进入
信号量等待队列的顺序竞争，没有为前台预留槽位；等槽位超过 `connect_timeout_seconds`
且本次工具调用尚未发出任何 SQL 时，退还执行预算并向 Agent 返回 busy 与剩余再试次数。
按 [ARCHITECTURE C8](../ARCHITECTURE.md#6-只读查询保护)，同轮同集群最多再试 1 次；
唯一再试在途时拒绝该集群新调用，成功不复位额度，再次繁忙后本轮该集群不再试。
这不改变后台刷新，不自动重放已开始的 SQL；后续新提问独立计算。

**函数名单按下表填写。** SQLGuard 先解析 SQL，再逐个检查函数；标有“或”的行接受所列别名，其他行仍按解析后的名称填写：

| 写法 | 名单中需要的名字 |
| --- | --- |
| `IFNULL(...)` | `COALESCE` |
| `SUBSTR(...)` | `SUBSTRING` |
| `DATE(...)`、`YEAR/MONTH/DAY/WEEK(...)` | `TS_OR_DS_TO_DATE`，以及对应的 `YEAR` 等 |
| `DATE_FORMAT(...)` | `DATE_FORMAT`，或同时列出 `TIME_TO_STR`、`TS_OR_DS_TO_TIMESTAMP` |
| `CURDATE()` | `CURRENT_DATE` |
| `FROM_UNIXTIME(x)` | `FROM_UNIXTIME` 或 `UNIX_TO_TIME` |
| `STR_TO_DATE(...)` | `STR_TO_DATE` 或 `STR_TO_TIME` |
| `DATE_ADD(..., INTERVAL n unit)`、`DATE_SUB(..., INTERVAL n unit)` | `DATE_ADD`、`DATE_SUB` |
| `DATEDIFF(a, b)` | `DATEDIFF`（规范化后仍为 `DATEDIFF`） |
| `DATE_TRUNC('day', a)` | `DATE_TRUNC` 或 `TIMESTAMP_TRUNC` |
| `APPROX_COUNT_DISTINCT(...)` | `APPROX_DISTINCT` |

`IFNULL` 仍需填写 `COALESCE`。S1 起 SQLGuard 检查原文，Adapter 执行原文（只可去尾分号、追加或下调顶层 LIMIT），因此获准的 `FROM_UNIXTIME(x, 格式)` 可以执行；4.1.4 本地对照已覆盖 `%i` 格式符。函数白名单仍逐个核对。物理表须写 `库.表`，缺库名时根据工具给出的可读结构快照候选修正，执行时仍复核当前权限；多库同名先澄清。

R2 的 StarRocks 4.1.4 合成数据曾实测：`INTERVAL 3 HOUR` 与规范化的 `INTERVAL '3' HOUR` 等价；`GROUP_CONCAT` 省略分隔符与补写 `SEPARATOR ','` 等价（含 NULL、重复值、DISTINCT 与排序样例）。R2 数据范围格式升为 `/8`。S1 不再重打印业务 SQL，也不升级该摘要；旧普通查询 Evidence 因新增 LIMIT 策略字段失效，旧计划、结构和慢查询事实继续按当前权限复核，无应用表迁移。

模板名单经测试覆盖常用聚合、数值、
条件、日期、字符串和窗口函数。日期加减只接受整数常量区间，单位限 `SECOND`、`MINUTE`、
`HOUR`、`DAY`、`WEEK`、`MONTH`、`YEAR`；动态区间、未知单位和子查询在 I/O 前拒绝。
`DATE_ADD`、`DATE_SUB`、`ADDDATE`、`SUBDATE`、`DATE_TRUNC`、`DATEDIFF` 都只接受两个
顶层实参，缺少或多出实参会在执行前拒绝，不会静默丢弃。
`DATE_DIFF` 不因允许 `DATEDIFF` 而自动放行；两种写法须分别验证其语义。

### 会话轮数的手动配置

用户 2026-10-11 批准的 S1 已将新模板从 20 调为 100 轮。已有部署的
`/opt/xiaowei/current/xiaowei.json` 需由操作者只把
`session_limits.max_history_turns` 改为 `100`；镜像升级不会自动修改该配置。
不要用模板覆盖自己的集群、身份或飞书配置。`session_limits.max_history_bytes`、
`retention_seconds` 和模型请求上限保持原值（主模板为 400000 字节、604800 秒和 1000000 字节）。
100 计数成功提交的交互轮次，失败轮次不增加这个计数；不是单轮模型步骤或消息条数。
较长结果可能先触发历史、请求、MCP 接收或权限复核上限，不保证所有对话都能聊到 100 轮。

仅提高轮数且其他绑定不变时，有效的 active 会话仍须完整回放和当前权限复核，符合条件可继续；
sealed/closed 会话不会复活，需 Web 点“新建会话”或飞书发 `/新建`。S1 不迁移数据库，
单独回退此调整只需恢复原轮数；已达到较低限额的 active 历史会在下一轮拒绝，不自动裁剪。
跨 schema 的镜像回退仍按本文成对恢复流程执行。

合成容量测量不证明真实模型的长排障效果；该效果未验证。S2 已实现安全原因展示，提前提醒仍待 S3。
配置预检和重启按本文升级步骤执行；本段不授予部署、改公司配置或真实服务验证许可。

### 会话拒绝原因与历史快照

S2 使用应用表 **v8**：请求新增可空的 `session_state`，不改对话历史表、既有失败码、限额或 Agent
提示词。v6/v7 不能直接启动新版，须按[含迁移的升级](#含迁移的升级)执行 `storage upgrade`；
发行清单只允许 v8 直接启动，v1–v7 升级。已绑定摘要密钥的 v6/v7 使用原密钥，不重新绑定。

新失败保留 `reason=session_unavailable`，并追加闭集的 `session_reject`：

| 原因码 | 用户的下一步 |
| --- | --- |
| `history_turn_limit` | 已提交轮数触顶；新建会话，补充目标和关键线索 |
| `history_bytes_limit` | 完整回放触顶；新建会话。提交阶段超限会明确说回答未保存/交付，已执行工具不会自动重试 |
| `expired` | 超过 PolicySession 固定保留期限；新建会话 |
| `binding_changed` | 模型或数据策略已变化；新建会话 |
| `unavailable` | 历史、归属或证据不能安全继续；新建会话，已执行操作不自动重放 |

Web 点“新建会话”，飞书发 `/新建`；新上下文不等于立即删除保存记录。显示的计数与时间标为
“本轮结束时”，刷新、重启或重复请求保持同一快照，不能当作当前剩余额度。无法安全计算的计数
不显示；旧请求空字段只显示通用回执，不根据现在的状态猜原因。损坏快照拒绝交付，关键保存失败
沿原有 readiness/重启恢复流程处理。无权或跨归属读取仍拒绝，系统提示不会绕开事实权限复核。

配置检查会拒绝无法放进 1024 字节快照的极端会话限额；常规 100 轮、400000 字节不受影响。
本轮运行期间失效而未完成提交前回放时，不显示开始时的旧字节数。提交后的快照编码或请求保存
失败记为 `result_not_saved` 并关闭会话，不记为模型失败，不自动重跑。

**回退：** v8 没有原地降级命令；旧镜像拒绝 v8。需要整镜像回退时，恢复迁移前的隔离数据库及
匹配旧镜像、配置、摘要密钥和控制文件，保留升级后的数据库取证；不能只把版本号改成 v7。
S2 的真实 Web/Lark 收发和公司升级尚未验收，部署须另获授权。

### 会话提前提醒

S3 复用 S2 的 v8 快照，不新增配置或迁移，也不修改已部署的轮数、字节、请求或期限上限。
成功结果在 Web/飞书同一条回复默认显示：已提交轮数或完整历史字节达到限额的 80%，
或距固定失效时间不超过 1 小时；多个条件合并。最后合法提交会提示下轮需新建。

提示始终指“本轮结束时”，历史读取、刷新、重启与操作者 `requests resend` 保留原观察，
不表示当前剩余额度，也不保证可继续的轮数；Evidence 或其他预算可能先到上限。
群内按共享会话计数，读取与重发请求仍核对原发起人、当前成员及数据权限。
Web 点击“新建会话”，飞书发送 `/新建`；关键线索须自己补充，新建不等于删除保存记录。

业务卡片保持卡片，提醒使用纯文本，计入原有字符、组件与最终请求体预算。
完整回复放不下时，固定容量说明与提醒同条发送，不自动拆分或补发；无法构造合规消息
则未发送并记 failed。发送开始后的超时/未知仍记 unknown，不自动重跑或补发。
提醒不进入模型、历史或工具结果，不触发额外回放/证据探测。

**回退：** 保留兼容 v8 的镜像和数据库时可单独撤回 S3 显示变更，S2 原有失败说明仍可用。
回到不支持 v8 的旧镜像仍按上一节成对恢复，不能只降镜像或改版本号。
真实 Lark 显示和公司部署未验收；本说明不授予部署许可。

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
此摘要与 PostgreSQL 应用表版本无关；本段配置修改不新增存储迁移。

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

`--bind-existing-digest-key` 对 v1–v5 只表示操作者确认这是原密钥，程序无法从旧库证明。
来源不明时不要执行。v6/v7→v8 已有密钥绑定，须先通过绑定校验，使用普通 `storage upgrade`
即可；保留这个标志也不会重绑或修补缺失/错误的绑定。v7 新增 Action 表及固定审批回执的请求失败码 CHECK，会话历史表不变。
v8 只新增请求的独立会话快照列；旧请求为 NULL，Action 表及失败码 CHECK 不改。
v8→v9 新增按会话/请求绑定的 PNG/JPEG 原图应用表，旧会话和请求不重写；须确认当前应用镜像支持 v9，再按上面的升级步骤执行。v9 备份包含原图，按原数据库备份的访问限制保存。
v9→v10 新增原生审批暂停检查点表与 Action 作废原因，旧 pending Action 作废、需重新提出；v8 可一次升级到 v10。
v10→v11 新增飞书报告文件表；v11→v12 新增最小交付分项与请求语义版本，旧记录仍按旧送达语义读取。v1–v11可按当前发行清单显式升级到v12，只有v12可直接启动当前镜像。
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

Action 同样由 `storage cleanup` 按自身保留期分批清理；执行中记录先由启动恢复记为 unknown，
不能在执行中删除。不新增定时任务；正常升级前继续成对备份数据库与配置。

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

Vertex 路径额外记录每次通过请求校验、发往底层 transport 的 HTTP 尝试 `vertex_http` INFO 行：
`turn`、状态、请求字节数；本地请求拒绝单列为 `vertex_request_rejected`，不计 HTTP 尝试。成功时记录
`prompt_tokens`、`candidates_tokens`、`thoughts_tokens`，缺失写 `unknown`。429 只从结构化错误
详情提取固定字段；项目号可能占用合法字符，reason、domain、quota metric/id 只输出已知公开值，
其余写 `other`。响应在 transport 被拒绝时仍计收到的 429，但正文详情写 `unknown`。
`rate_kind=quota` 表示明确的配额详情，`no_quota_detail` 只表示
`RESOURCE_EXHAUSTED` 且未见配额详情，不能据此断定共享容量不足。`retry_after` 只记录合法数字秒
并按现有 20 秒采信上限截断，`retry_delay_s` 来自 `RetryInfo`，二者均不改变 SDK 重试。
每次 Vertex 轮次结束另有一行 `turn=... stage=model_summary`，记录 HTTP 尝试数、429 数、
实际重试次数、最大请求字节和成功响应的输入/输出 token 合计；任一成功响应缺少必要 usage 时
对应合计写 `unknown`。有 429 时只附最后一次的安全分类与字段。审批暂停及后续恢复各按该次
运行记汇总，使用相同原轮编号；真实生产 429 样本尚待观察验证。

PostgreSQL 16 同一大版本内更换镜像 digest 不在本轮验证范围，须另做备份、兼容和回退演练。
