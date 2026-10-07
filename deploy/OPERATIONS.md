# 小维容器运维

本说明适用于发行包里的 `compose.yaml` 与 `release.json`。部署只有小维和 PostgreSQL 两个容器；
Web 端口只发布到宿主机 `127.0.0.1`，PostgreSQL 不发布端口。Host/Origin 校验只防浏览器跨站
和 DNS rebinding，访问范围仍由端口发布与服务器网络保证。

建议使用固定目录 `/opt/xiaowei/current`。`.env`、`xiaowei.json` 与 `certs/` 由操作者持有，升级
只替换 `compose.yaml` 和 `release.json`；升级前留存的旧 JSON 配置（`.env` 的 `XW_CONFIG_FILE`
指向的文件，默认 `./xiaowei.json`）用于回退。旧发行包及旧应用镜像保留到新版验收完成。

## 获取 amd64 发行包与镜像权限

正式发行由 GitHub Actions 中的 `publish-amd64` 手动触发，且只接受 `main`。操作方法见
[GitHub 手动运行 workflow 说明](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)。它会从当前提交构建
`linux/amd64` 应用镜像，按镜像 digest 生成发行包，并附到同一提交的 GitHub Release。首次发布前，
仓库管理员需要设置 Repository variable `GHCR_USERNAME`（PAT 所属 GitHub 用户名）和 secret
`GHCR_WRITE_TOKEN`（classic PAT，仅勾选 `write:packages`；创建时若自动勾选 `repo`，请取消）。
GitHub Release 的写入使用 workflow 的 `GITHUB_TOKEN`。不要把 PAT 写入仓库文件或聊天。
镜像通过独立 PAT 发布且不自动关联源码仓库，首次发布默认应为 Private。

首次发布后，先在 GitHub Packages 页面确认 `xiaowei-agent-sdk` 包的可见性为 **Private**，再分发。
从有权读取仓库的电脑下载 Release 中的
`xiaowei-<完整代码 SHA>-linux-amd64.tar.gz`，再传到服务器；服务器不需要 Git、Python 或 GitHub CLI。

先在服务器创建安装目录：

```sh
mkdir -p /opt/xiaowei/current
```

在有仓库读取权限的电脑上下载 Release 归档后，用 SSH/SCP 传到服务器：

```sh
release_sha=PUT_40_CHAR_CODE_SHA_HERE
server=SERVER_HOST_OR_IP
scp "xiaowei-${release_sha}-linux-amd64.tar.gz" "root@${server}:/opt/xiaowei/"
```

服务器需要单独的 classic PAT，权限为 `read:packages`，且 PAT 所属账号必须能读取该私有包。
GitHub Packages [要求使用 classic PAT](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#authenticating-with-a-personal-access-token-classic)。首次在服务器登录时，Docker 会隐藏输入 token；后续
`compose pull` 会复用 Docker 凭据：

```sh
docker login ghcr.io --username <PAT所属GitHub用户名>
```

仅首次安装时展开到 `/opt/xiaowei/current`：

```sh
release_sha=PUT_40_CHAR_CODE_SHA_HERE
tar -xzf "/opt/xiaowei/xiaowei-${release_sha}-linux-amd64.tar.gz" \
  -C /opt/xiaowei/current
```

升级时把新归档展开到独立的 `/opt/xiaowei/releases/<完整代码SHA>`，再按下文普通升级步骤替换控制文件。
不要把未核验的归档或镜像标成已验收版本。

## 首次安装

1. 把发行包展开到固定目录，核对 `release.json` 的平台、代码 SHA 与两个镜像 digest。
2. 仅首次复制 `.env.example` 为 `.env`、`xiaowei.example.json` 为 `xiaowei.json`，并创建
   `certs/`。以后不能用新包里的模板覆盖它们。
3. JSON 中的 `listen_host` 保持 `127.0.0.1`；`listen_port`、
   `web.allowed_origins` 与 `.env` 的 `XW_WEB_PORT` 使用同一端口。自定义 CA 放在 `certs/`，
   JSON 写容器路径 `/etc/xiaowei/certs/<文件>`。
4. `.env` 只允许部署管理员读取；配置和 CA 须允许容器 UID 65532 只读。

两份配置的分工：`.env` 只放秘密和部署参数，可以用 `#` 写注释；`xiaowei.json` 放模型、StarRocks
地址、权限等非秘密内容，是标准 JSON，不能写 `#` 或 `//` 注释。

`xiaowei.example.json` 是最小配置：一个 Vertex 模型、一个 StarRocks 目标 `warehouse`、Web，飞书关闭
（`null`）。第二个目标和飞书需要时再按下文“增加第二个 StarRocks 目标”“启用飞书”加入。模型段里的
`vertex-main` 不需要填 API 地址、协议或输出方式，这些由程序固定，写了反而会被拒绝。

两份模板里的 `<……>` 以及 `replace-with-approved-vertex-model`、
`cli_replacewithappid`、`oc_replace_with_chat_id` 都是待填写标记，没替换时 `config check` 和
`serve` 都会按字段路径拒绝，不会启动。只有与模板原文完全相同的值才算没替换，真实值里含尖括号
不受影响；`.env` 的 `XW_POSTGRES_PASSWORD` 仍是模板原文时同样拒绝。

| 位置 | 是否必填 | 填什么 | 从哪里拿 | 以后能否改 |
| --- | --- | --- | --- | --- |
| `.env` 的 `XW_POSTGRES_PASSWORD` 与 `XW_DATABASE_URL` | 必填 | 自己生成的数据库密码；URL 里的密码与它一致，特殊字符按 URI 编码 | `openssl rand -hex 24` | 初始化后不改 |
| `.env` 的 `XW_DIGEST_KEY` | 必填 | 自己生成的摘要密钥 | `openssl rand -hex 32` | 永不重新生成；与数据库成对备份 |
| `.env` 的 `XW_MODEL_API_KEY` | 必填 | Vertex AI 的 API Key 本身（Express Mode），不是服务账号 JSON 文件或项目号 | Google Cloud 管理员 | 可轮换，改后重启 |
| `.env` 的 `XW_STARROCKS_PASSWORD` | 必填 | 第一个目标只读账号的**密码** | StarRocks 管理员 | 可轮换，改后重启 |
| `.env` 的 `XW_ARCHIVE_STARROCKS_PASSWORD` | 按需 | 加了第二个目标时去掉行首 `#`，填它的只读密码 | StarRocks 管理员 | 可轮换 |
| `.env` 的 `XW_FEISHU_APP_SECRET` | 按需 | 启用飞书时去掉行首 `#`，填应用 App Secret | 飞书开放平台 | 可轮换 |
| JSON 的 `model.model` | 必填 | 获准的 Vertex 模型 ID，只含字母、数字、`.`、`_`、`-`，例如计划验收的 `gemini-3-flash-preview`（真实验收尚未完成） | 模型服务管理员 | 换模型后须新建会话，并重新运行 `model check` |
| JSON 的 `targets[].description`、`starrocks.host`、`database`、`user` | 必填 | 集群用途说明、FE 地址、默认库、只读账号名 | StarRocks 管理员 | 可改，改后重启 |
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

操作者电脑通过 SSH 转发访问；调整端口时同时调整 `.env`、JSON 和转发命令：

```sh
ssh -N -L 127.0.0.1:8501:127.0.0.1:8501 <获准服务器>
```

不得把 Compose 发布行改成省略 host IP 的 `8501:8501`，也不得使用 host network。

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

然后经 SSH 转发检查 `/readyz`。本机 P3-B 只验证了停止小维服务后用指定服务名执行
`up -d xiaowei`；全栈停止后执行上面的 `up -d`、真实宿主机或 Docker 重启仍须在 P3-C 实测。

## 普通升级与回退（schema 不变）

先把新包展开到 `/opt/xiaowei/releases/<新 SHA>`。查看当前和新版 `release.json`，并从当前
PostgreSQL 读取 schema：

```sh
cd /opt/xiaowei/current
cat release.json
cat /opt/xiaowei/releases/<新 SHA>/release.json
docker compose --env-file .env exec -T postgres sh -c \
  'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SELECT version FROM xiaowei_schema_version"'
```

只有数据库版本列在新版 `direct_start_schema_versions` 时才走普通升级。先留存旧控制文件和当前
使用的 JSON 配置（JSON 含单聊用户的 open_id，只允许部署管理员读取），再拉取并预检新版；这些命令
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
docker compose --env-file .env -f compose.next.yaml pull xiaowei
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

如果新版启动失败或需要回退，先用当前（候选）Compose 停止小维，停止成功后才恢复旧控制文件、旧
digest 和升级前的 JSON 配置，再按旧镜像预检并启动；停止失败时命令链中止，不覆盖任何文件。停止后
到旧版就绪前服务不可用。新版接受的配置旧版不一定能读，例如旧版要求 `feishu.users` 至少一人，
新版允许 `{}`；升级后在 JSON 里做的修改会随回退撤销，需要时按旧版规则重新填写。`.env`、CA 与数据库不改（本次升级没有新增 `.env` 项，
不要用新包模板覆盖 `.env`）：

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

备份会同时包含 SDK Session 表和 `xiaowei_*` 应用表。把备份复制到其他受控存储；仅保留本机卷
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
Compose 的 `XW_LOG_MAX_SIZE`/`XW_LOG_MAX_FILES` 限制本地日志轮转。

PostgreSQL 16 同一大版本内更换镜像 digest 不在本轮验证范围，须另做备份、兼容和回退演练。
