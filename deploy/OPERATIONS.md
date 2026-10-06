# 小维 P3-A 容器运维

本说明适用于同一发行包内的 `compose.yaml` 与 `release.json`。首版只验证 Linux 容器、Docker
Compose、一个小维容器和一个 PostgreSQL 16 容器。Web 端口固定发布到宿主机 `127.0.0.1`；
PostgreSQL 不发布宿主端口。Host/Origin 校验只能防浏览器跨站和 DNS rebinding，不能代替网络隔离。

## 首次准备

1. 在固定部署目录展开发行包，核对 `release.json` 中的平台、代码 SHA 和两个镜像 digest。操作时不要在 shell 中导出同名 `XW_*` 变量，避免其按 Compose 优先级覆盖 `.env`。
2. 仅首次执行：复制 `.env.example` 为 `.env`、复制 `xiaowei.example.json` 为 `xiaowei.json`，创建
   `certs/`。以后升级保留这三个操作者资源，不从新包覆盖。
3. 编辑 `.env` 和 `xiaowei.json`。JSON 的 `listen_host` 保持 `127.0.0.1`；`listen_port`、
   `web.allowed_origins` 的端口与 `.env` 的 `XW_WEB_PORT` 相同。自定义 CA 放在 `certs/`，JSON 中写
   容器路径 `/etc/xiaowei/certs/<文件>`。
4. 让 `.env` 仅部署管理员可读；让 `xiaowei.json` 和所需 CA 可由容器 UID 65532 只读。

```sh
chmod 600 .env
mkdir -p certs
docker compose --env-file .env config --quiet
docker compose --env-file .env run --rm --no-deps xiaowei config check
```

`config check` 不连接 PostgreSQL、模型、StarRocks 或飞书，也不写文件。它只证明 JSON、引用的环境值、
CA、端口和停止宽限相互一致；不证明外部服务可用。命令返回 2 时先修配置，不启动或替换旧服务。

## 初始化与启动

只对全新且专用于小维的卷执行一次 `storage init`：

```sh
docker compose --env-file .env up -d postgres --wait
docker compose --env-file .env run --rm --no-deps xiaowei storage init
docker compose --env-file .env up -d xiaowei --wait
docker compose --env-file .env ps
```

浏览器从操作者电脑经 SSH 转发访问：

```sh
ssh -N -L 127.0.0.1:8501:127.0.0.1:8501 <获准服务器>
```

若调整 `XW_WEB_PORT`，同时调整 JSON 和 SSH 两端端口。不得把 Compose 发布行改成省略 host IP 的
`8501:8501`，不得使用 host network。公司网络上仍须从另一台同二层主机实测该端口不可达。

## 日常状态与停止

```sh
docker compose --env-file .env ps
docker compose --env-file .env logs --tail 200 xiaowei
docker compose --env-file .env stop xiaowei
docker compose --env-file .env down
```

`down` 不加 `-v`，命名卷会保留。持久卷不是备份。P3-A 尚未完成 A→B 不同镜像升级、回退与
`pg_dump`/`pg_restore` 恢复演练；在 P3-B 验证前，不把重建同一镜像称为已验证升级。

小维容器当前使用有限的 `on-failure:3`，PostgreSQL 使用 `unless-stopped`。P3-A 尚未验证宿主机或
Docker 重启后小维是否自动启动；发生这类重启后，操作者须执行 `docker compose --env-file .env up -d`
并检查 `ps`/就绪状态。P3-B 会实测并确定最终维护步骤，在此之前不承诺自动恢复服务。

## 摘要密钥与旧库

数据库保存摘要密钥的不可逆指纹，不保存密钥。密钥错配、指纹缺失或损坏时，启动和维护命令都会
在业务处理前拒绝。不得通过清空卷或生成新密钥修复；`.env` 与数据库备份必须成对保存。

旧 schema v1-v5 默认不绑定密钥。只有确认当前 `.env` 仍使用原部署密钥并已完成配套备份后，才可
执行 `storage upgrade --bind-existing-digest-key`。该标志不能证明密钥正确，也不能覆盖 v6 指纹；
来源不明时保持旧库不动。
