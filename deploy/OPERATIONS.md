# 小维容器运维

本说明适用于发行包里的 `compose.yaml` 与 `release.json`。部署只有小维和 PostgreSQL 两个容器；
Web 端口只发布到宿主机 `127.0.0.1`，PostgreSQL 不发布端口。Host/Origin 校验只防浏览器跨站
和 DNS rebinding，访问范围仍由端口发布与服务器网络保证。

建议使用固定目录 `/opt/xiaowei/current`。`.env`、`xiaowei.json` 与 `certs/` 由操作者持有，升级
只替换 `compose.yaml` 和 `release.json`。旧发行包及旧应用镜像保留到新版验收完成。

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

```sh
cd /opt/xiaowei/current
chmod 600 .env
mkdir -p certs
docker compose --env-file .env config --quiet
docker compose --env-file .env run --rm --no-deps xiaowei config check
docker compose --env-file .env up -d postgres --wait
docker compose --env-file .env run --rm --no-deps xiaowei storage init
docker compose --env-file .env up -d xiaowei --wait
docker compose --env-file .env ps
```

`config check` 不连接 PostgreSQL、模型、StarRocks 或飞书，也不写文件。它返回 2 时先修配置，
不要停止或替换正在运行的旧服务。`storage init` 只对全新、专用于小维的数据库执行一次。

操作者电脑通过 SSH 转发访问；调整端口时同时调整 `.env`、JSON 和转发命令：

```sh
ssh -N -L 127.0.0.1:8501:127.0.0.1:8501 <获准服务器>
```

不得把 Compose 发布行改成省略 host IP 的 `8501:8501`，也不得使用 host network。

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

只有数据库版本列在新版 `direct_start_schema_versions` 时才走普通升级。先拉取并预检新版；这些
命令失败不会停止旧服务：

```sh
cd /opt/xiaowei/current
cp /opt/xiaowei/releases/<新 SHA>/compose.yaml compose.next.yaml
cp /opt/xiaowei/releases/<新 SHA>/release.json release.next.json
docker compose --env-file .env -f compose.next.yaml pull xiaowei
docker compose --env-file .env -f compose.next.yaml config --quiet
docker compose --env-file .env -f compose.next.yaml run --rm --no-deps xiaowei config check
```

预检通过后保存旧控制文件，再只重建小维；PostgreSQL 容器和命名卷不重建：

```sh
cd /opt/xiaowei/current
previous=/opt/xiaowei/releases/<旧 SHA>
mkdir -p "$previous"
cp compose.yaml release.json "$previous"/
mv compose.next.yaml compose.yaml
mv release.next.json release.json
docker compose --env-file .env up -d --no-deps --force-recreate xiaowei --wait
docker compose --env-file .env ps
```

如果新版启动失败，恢复旧控制文件和旧 digest；`.env`、JSON、CA 与数据库均不改：

```sh
cd /opt/xiaowei/current
cp /opt/xiaowei/releases/<旧 SHA>/compose.yaml compose.yaml
cp /opt/xiaowei/releases/<旧 SHA>/release.json release.json
docker compose --env-file .env up -d --no-deps --force-recreate xiaowei --wait
```

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
