"""正式运行装配：一份可信配置 → 单实例锁 → 启动恢复 → 运行对象 → Web 与可选飞书 → 有界停止。

``open_runtime`` 是唯一装配顺序：配置校验 → PostgreSQL 引擎 → 专用连接取得实例锁 → schema 检查与
启动一致性恢复 → 每个目标的 StarRocks Adapter、结构快照与受治理工具 → 唯一授权来源 → Evidence /
Governance → 各目标首次结构刷新（失败只让该目标暂不可用）与后台定时刷新 →
模型绑定 → Application → ``ChannelService``。任一步失败时按相反顺序关闭已创建的资源并释放锁；
启动失败不对外服务，而不是带着未就绪状态继续运行。

``serve`` 在此之上运行 Web（Uvicorn，监听套接字先由本模块绑定，只提供 HTTP）与可选飞书。运行中
持锁连接终止时立即锁低 readiness（连接终止通知，周期核对兜底）并退出；新请求的接收在数据库内与
其他实例的接管恢复互斥（见 ``storage.InstanceLock.admits``）。停止顺序：Web 停止接收 →
飞书 ``drain``（停止接收，有界等待在途与队列）→ 取消消费者 → 关闭长连接；各步都有期限，任一步
未在期限内完成或出现异常时返回非零退出码。飞书长连接在启动时连不上只让飞书不可用（``/readyz``
标明），Web 照常服务。

维护命令：``initialize`` / ``upgrade`` 独占同一把实例锁；``cleanup`` 与 ``resend`` 不执行恢复，只靠
数据库条件更新与在线 ``serve`` 并发。``resend`` 只重发飞书 failed/unknown 的已保存结果：不装配模型、
不连接 StarRocks，经同一 ``EvidenceStore`` 重验与投递 CAS 后发送一次。

凭据只以 ``env:NAME`` 引用出现在配置中，在用到它的装配步骤才解析；错误信息不含凭据、连接串或
上游原文。``model_transport``、``starrocks_connect``（按目标 ID）、``feishu_channel`` 只供测试替换
最底层 I/O，治理、证据与状态路径不变。
"""

import asyncio
import logging
import math
import socket
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx2
import uvicorn
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from xiaowei.app import AppConfig, Application, BusinessContext, DataPolicy, TargetInfo
from xiaowei.channel import (
    AccessDecision,
    ChannelService,
    RequestRef,
    ResultDelivery,
)
from xiaowei.channel_store import ChannelStore, RecoveryReport, SendOutcome
from xiaowei.config import (
    FeishuConfig,
    WebConfig,
    configure_runtime,
    is_secret_ref,
    resolve_secret_ref,
)
from xiaowei.evidence import EvidenceStore
from xiaowei.feishu import FeishuGateway, LarkChannel, LarkTransport, lark_channel, render
from xiaowei.governance import GovernedTools, ToolCatalog
from xiaowei.model_api import ModelProfile, open_model
from xiaowei.models import (
    AUDIENCES,
    Audience,
    Budget,
    Channel,
    ClusterId,
    Delivery,
    Identity,
    Label,
    ToolId,
)
from xiaowei.session import CleanupReport, SessionLimits, cleanup_expired
from xiaowei.starrocks import (
    Connection,
    Connector,
    StarRocksAdapter,
    StarRocksTarget,
    open_starrocks,
)
from xiaowei.starrocks_schema import SchemaCache
from xiaowei.starrocks_tools import (
    AUDIT_TOOLS,
    DIAGNOSE_TOOLS,
    QUERY_TOOLS,
    StarRocksTools,
    starrocks_tools,
)
from xiaowei.storage import (
    Backend,
    InstanceLock,
    Readiness,
    StorageUnavailableError,
    check_storage,
    hold_backend,
    hold_instance_lock,
    initialize_storage,
    open_engine,
    upgrade_storage,
)
from xiaowei.web import create_web_app

logger = logging.getLogger(__name__)

_LOOPBACK = frozenset({"127.0.0.1", "::1"})


class ConfigError(Exception):
    """配置文件不可读或不符合契约；消息只含字段路径与固定说明，不含配置值。"""


# ---- 配置 ----------------------------------------------------------------------------


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _secret_ref(value: str) -> str:
    if not is_secret_ref(value):
        raise ValueError("必须是 env:NAME 形式的凭据引用")
    return value


class StorageConfig(_Config):
    """PostgreSQL 与应用状态：连接串和摘要密钥都只以引用出现；保留期与回答上限。"""

    database_url_ref: str
    digest_key_ref: str
    evidence_retention_seconds: int = Field(gt=0)
    request_retention_seconds: int = Field(gt=0)
    max_answer_bytes: int = Field(gt=0)

    _refs = field_validator("database_url_ref", "digest_key_ref")(_secret_ref)


class AccessConfig(_Config):
    """唯一授权来源的静态表：内部 subject → 获准工具；``policy_version`` 进入请求摘要。"""

    policy_version: Label
    grants: dict[Label, frozenset[ToolId]] = Field(min_length=1)


class TargetConfig(_Config):
    """一个查询目标：类型（首版只有 ``starrocks``）、交给模型的用途说明与业务口径、连接与范围。

    集群 ID 就是 ``starrocks.target_id``；连接地址、账号与凭据引用只用于装配，不交给模型。
    """

    type: Literal["starrocks"]
    description: str = Field(min_length=1, max_length=500)
    business_context: BusinessContext | None
    starrocks: StarRocksTarget

    @property
    def target_id(self) -> str:
        return self.starrocks.target_id

    @field_validator("starrocks")
    @classmethod
    def _cluster_id(cls, value: StarRocksTarget) -> StarRocksTarget:
        try:
            _CLUSTER_ID.validate_python(value.target_id)
        except ValidationError:
            raise ValueError(
                "target_id 必须是 1–32 位小写字母、数字、下划线或连字符，以字母或数字开头"
            ) from None
        return value


_CLUSTER_ID: TypeAdapter[str] = TypeAdapter(ClusterId)

# 旧单目标配置的顶层字段：不再双读，报告到 targets 的迁移方式。
_LEGACY_KEYS = ("starrocks", "business_context")
_LEGACY_HINT = (
    "单目标配置已不再支持：把 starrocks 改写为 targets 中的一项"
    '（{"type": "starrocks", "description": ..., "business_context": 原 business_context 去掉 '
    'target_id 或 null, "starrocks": 原 starrocks}），集群 ID 取 starrocks.target_id'
)


class ServeConfig(_Config):
    """一个部署的全部可信配置：单模型 Profile、一个或多个查询目标、Web 与可选飞书。"""

    storage: StorageConfig
    model: ModelProfile
    data_policy: DataPolicy
    session_limits: SessionLimits
    budget: Budget
    max_concurrent_turns: int = Field(gt=0)
    targets: tuple[TargetConfig, ...] = Field(min_length=1)
    projection_bytes: dict[Audience, Annotated[int, Field(gt=0)]]
    access: AccessConfig
    web: WebConfig
    listen_host: Annotated[str, StringConstraints(min_length=1)] = "127.0.0.1"
    listen_port: int = Field(default=8501, ge=1, le=65535)
    feishu: FeishuConfig | None = None
    shutdown_timeout_seconds: float = Field(gt=0, le=300)
    lock_check_seconds: float = Field(default=5, gt=0, le=60)

    @model_validator(mode="before")
    @classmethod
    def _not_legacy(cls, data: object) -> object:
        if isinstance(data, Mapping) and any(key in data for key in _LEGACY_KEYS):
            raise ValueError(_LEGACY_HINT)
        return data

    @field_validator("targets")
    @classmethod
    def _distinct_targets(cls, value: tuple[TargetConfig, ...]) -> tuple[TargetConfig, ...]:
        ids = [t.target_id for t in value]
        if len(set(ids)) != len(ids):
            raise ValueError("targets 中的集群 ID 不能重复")
        return value

    @field_validator("listen_host")
    @classmethod
    def _loopback(cls, value: str) -> str:
        if value not in _LOOPBACK:
            raise ValueError("首版只监听 loopback 地址")
        return value

    @field_validator("projection_bytes")
    @classmethod
    def _all_audiences(cls, value: dict[Audience, int]) -> dict[Audience, int]:
        if set(value) != set(AUDIENCES):
            raise ValueError("必须且只能给出四种用途的容量")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "ServeConfig":
        registered = _registered_tools(self)
        if not self.data_policy.model_tools <= registered:
            raise ValueError("data_policy.model_tools 只能包含已登记的 StarRocks 工具")
        for tools in self.access.grants.values():
            if not tools <= registered:
                raise ValueError("access.grants 只能授予已登记的 StarRocks 工具")
        storage = self.storage
        if storage.request_retention_seconds > storage.evidence_retention_seconds:
            raise ValueError("可重发结果的保留期不得长于证据保留期")
        # 正式监听不配置 TLS，浏览器发送的 Origin 必然是 http://；同址 HTTPS 不能代替它。
        netloc = f"[{self.listen_host}]" if ":" in self.listen_host else self.listen_host
        if f"http://{netloc}:{self.listen_port}" not in self.web.allowed_origins:
            raise ValueError("web.allowed_origins 必须包含本进程实际监听的 http://host:port")
        if self.feishu is not None:
            if self.web.operator_id in self.feishu.users.values():
                raise ValueError("Web 操作者与飞书用户不能使用同一个内部 subject")
            if self.feishu.consumer_count > self.max_concurrent_turns:
                raise ValueError("feishu.consumer_count 不得超过 max_concurrent_turns")
        return self


def load_config(path: Path) -> ServeConfig:
    """读取 JSON 配置；失败时只报告字段路径与固定说明，不回显配置值。

    用 JSON 而不是 TOML：Profile 与 StarRocks 目标中有必须显式写出的可空字段（如
    ``reasoning_effort``、``tls_ca_file``），TOML 无法表达 null。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        raise ConfigError("配置文件不可读") from None
    try:
        return ServeConfig.model_validate_json(raw)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {_reason(error)}"
            for error in exc.errors(include_url=False, include_input=False)
        ]
        raise ConfigError("配置不符合要求：" + "；".join(problems)) from None


def _reason(error: Mapping[str, object]) -> str:
    """Pydantic 内置错误的说明不含输入值；自定义校验只给固定文字。"""
    if error.get("type") == "value_error":
        context = error.get("ctx")
        cause = context.get("error") if isinstance(context, Mapping) else None
        return str(cause) if cause is not None else "取值不符合要求"
    return str(error.get("msg", "取值不符合要求"))


def _registered_tools(config: ServeConfig) -> frozenset[str]:
    """装配时会登记的 StarRocks 工具：只有配置了审计源的目标登记慢查询工具。"""
    audited = any(t.starrocks.audit is not None for t in config.targets)
    return QUERY_TOOLS | (AUDIT_TOOLS if audited else frozenset())


def _app_config(config: ServeConfig) -> AppConfig:
    audit = _registered_tools(config) - QUERY_TOOLS
    return AppConfig(
        purposes={"query": QUERY_TOOLS | audit, "diagnose": DIAGNOSE_TOOLS | audit},
        data_policies={config.model.data_policy_id: config.data_policy},
        session_limits=config.session_limits,
        max_concurrent_turns=config.max_concurrent_turns,
        targets=tuple(
            TargetInfo(
                target_id=t.target_id,
                description=t.description,
                business_context=t.business_context,
            )
            for t in config.targets
        ),
    )


# ---- 唯一授权来源 ----------------------------------------------------------------------


class StaticAccess:
    """配置中的授权表：入口解析（``resolve``）与 Evidence 授权（``authorize``）读同一份表。

    subject 与渠道无关：Web 操作者与飞书用户由各自适配器映射为内部 subject，配置校验保证二者
    不重名。获准用户得到全部已配置目标；某目标没有登记的工具由工具目录在调用前拒绝。
    """

    def __init__(self, config: AccessConfig, target_ids: frozenset[str]) -> None:
        self._grants: Mapping[str, frozenset[str]] = dict(config.grants)
        self._version = config.policy_version
        self._targets = target_ids

    async def resolve(self, channel: Channel, subject_id: str) -> AccessDecision | None:
        tools = self._grants.get(subject_id)
        if not tools:
            return None
        return AccessDecision(
            subject_id=subject_id,
            target_ids=self._targets,
            authorized_tools=tools,
            policy_version=self._version,
        )

    async def authorize(self, identity: Identity, target_id: str, tool_id: str) -> bool:
        return target_id in self._targets and tool_id in self._grants.get(
            identity.subject_id, frozenset()
        )


# ---- 装配 ----------------------------------------------------------------------------


def now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Runtime:
    """一次装配的运行对象；只在 ``open_runtime`` 的上下文内有效。"""

    engine: AsyncEngine
    lock: InstanceLock
    readiness: Readiness
    recovery: RecoveryReport
    service: ChannelService


@dataclass(frozen=True)
class _Delivery:
    store: ChannelStore
    evidence: EvidenceStore
    results: ResultDelivery
    tools: StarRocksTools


def _delivery(
    config: ServeConfig,
    engine: AsyncEngine,
    schemas: Mapping[str, SchemaCache],
    readiness: Readiness,
    clock: Callable[[], datetime],
    sender: Backend | None = None,
) -> _Delivery:
    """请求存储、工具目录、唯一授权来源、Evidence 与交付；serve 与显式重发共用同一装配。

    ``sender`` 只由显式重发传入：它占用的连接身份记入投递尝试（见 ``ChannelStore``）。
    """
    storage = config.storage
    store = ChannelStore(
        engine,
        key=resolve_secret_ref(storage.digest_key_ref),
        clock=clock,
        readiness=readiness,
        request_retention_seconds=storage.request_retention_seconds,
        session_retention_seconds=config.session_limits.retention_seconds,
        evidence_retention_seconds=storage.evidence_retention_seconds,
        max_answer_bytes=storage.max_answer_bytes,
        sender=sender,
    )
    first, *others = (
        starrocks_tools(
            schemas[t.target_id].adapter, config.projection_bytes, schema=schemas[t.target_id]
        )
        for t in config.targets
    )
    tools = first
    for other in others:
        tools += other
    access = StaticAccess(config.access, frozenset(schemas))
    evidence = EvidenceStore(
        engine,
        ToolCatalog(tools.contracts, tools.policies),
        authorize=access.authorize,
        clock=clock,
        retention_seconds=storage.evidence_retention_seconds,
    )
    results = ResultDelivery(store, evidence, access, budget=config.budget)
    return _Delivery(store, evidence, results, tools)


@asynccontextmanager
async def _engine(config: ServeConfig) -> AsyncIterator[AsyncEngine]:
    async with open_engine(resolve_secret_ref(config.storage.database_url_ref)) as engine:
        yield engine


@asynccontextmanager
async def _refreshing(schemas: Mapping[str, SchemaCache]) -> AsyncIterator[None]:
    """各目标先并行刷新一次结构快照（失败只让该目标暂不可用，不阻止启动），再后台定时刷新。

    首次刷新受各目标 ``refresh_timeout_seconds`` 约束；退出时取消后台循环与进行中的刷新并等待。
    """
    loops: list[asyncio.Task[None]] = []
    try:
        await asyncio.gather(*(schema.refresh() for schema in schemas.values()))
        loops = [
            asyncio.create_task(schema.run(), name=f"xiaowei-schema-loop-{target}")
            for target, schema in schemas.items()
        ]
        yield
    finally:
        # 启动中途被取消时，共享的刷新任务不随等待者取消：同样在这里取消并等待。
        for task in loops:
            task.cancel()
        if loops:
            await asyncio.wait(loops)
        for schema in schemas.values():
            await schema.aclose()


@asynccontextmanager
async def open_runtime(
    config: ServeConfig,
    *,
    clock: Callable[[], datetime] = now,
    model_transport: httpx2.AsyncBaseTransport | None = None,
    starrocks_connect: Mapping[str, Connector] | None = None,
) -> AsyncIterator[Runtime]:
    """按唯一顺序装配；退出或任一步失败时逆序关闭并释放实例锁。"""
    configure_runtime()
    readiness = Readiness()
    async with AsyncExitStack() as stack:
        engine = await stack.enter_async_context(_engine(config))
        lock = await stack.enter_async_context(hold_instance_lock(engine, readiness))
        await check_storage(engine)
        schemas = {
            t.target_id: SchemaCache(
                open_starrocks(t.starrocks, clock=clock)
                if starrocks_connect is None
                else StarRocksAdapter(
                    t.starrocks, connect=starrocks_connect[t.target_id], clock=clock
                ),
                clock=clock,
            )
            for t in config.targets
        }
        parts = _delivery(config, engine, schemas, readiness, clock)
        recovery = await parts.store.recover(lock)
        logger.info(
            "启动恢复：interrupted=%d unknown=%d unsent=%d",
            recovery.interrupted,
            recovery.unknown,
            recovery.unsent,
        )
        await stack.enter_async_context(_refreshing(schemas))
        model = await stack.enter_async_context(open_model(config.model, transport=model_transport))
        app = Application(
            _app_config(config),
            model=model,
            engine=engine,
            governance=GovernedTools(parts.evidence),
            local_tools=parts.tools.executes,
            clock=clock,
        )
        service = ChannelService(app, parts.results)
        yield Runtime(engine, lock, readiness, recovery, service)


# ---- serve ---------------------------------------------------------------------------


class ListenError(Exception):
    def __init__(self, host: str, port: int) -> None:
        super().__init__(f"无法监听 {host}:{port}（端口被占用或地址不可用）")


def _bind(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        return socket.create_server((host, port), family=family)
    except OSError:
        raise ListenError(host, port) from None


async def _watch(lock: InstanceLock, interval: float, stopping: asyncio.Event) -> None:
    """等待持锁连接的终止通知（通知时 readiness 已锁低），返回即触发停止。

    周期核对只是兜底：网络中断而本端未收到连接关闭时，由下一次 ``verify`` 发现。正常停止经
    ``stopping`` 通知退出，不在核对查询中途取消（取消会使持锁连接失效，被误判为丢锁）。
    """
    lost = asyncio.create_task(lock.lost.wait())
    stop = asyncio.create_task(stopping.wait())
    try:
        while True:
            done, _ = await asyncio.wait(
                {lost, stop}, timeout=interval, return_when=asyncio.FIRST_COMPLETED
            )
            if lost in done:
                break
            if stop in done:
                return
            try:
                await lock.verify()
            except StorageUnavailableError:
                break
    finally:
        for task in (lost, stop):
            task.cancel()
    logger.error("实例锁连接已断开，停止服务")


@dataclass
class _Feishu:
    gateway: FeishuGateway
    transport: LarkTransport
    consumers: asyncio.Task[None]


async def serve(
    config: ServeConfig,
    *,
    stop: asyncio.Event,
    clock: Callable[[], datetime] = now,
    model_transport: httpx2.AsyncBaseTransport | None = None,
    starrocks_connect: Mapping[str, Connector] | None = None,
    feishu_channel: LarkChannel | None = None,
) -> int:
    """运行到 ``stop`` 被设置、持锁连接丢失或某个组件意外结束；返回进程退出码（0 为正常停止）。"""
    sock = _bind(config.listen_host, config.listen_port)
    try:
        async with open_runtime(
            config,
            clock=clock,
            model_transport=model_transport,
            starrocks_connect=starrocks_connect,
        ) as runtime:
            return await _serve(config, runtime, sock, stop, clock, feishu_channel)
    finally:
        sock.close()


async def _serve(
    config: ServeConfig,
    runtime: Runtime,
    sock: socket.socket,
    stop: asyncio.Event,
    clock: Callable[[], datetime],
    feishu_channel: LarkChannel | None,
) -> int:
    components: dict[str, str] = {"feishu": "disabled"}
    feishu = await _start_feishu(config, runtime, clock, feishu_channel, components)
    app = create_web_app(runtime.service, config.web, components=lambda: components)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            lifespan="off",
            log_config=None,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            timeout_graceful_shutdown=math.ceil(config.shutdown_timeout_seconds),
        )
    )
    web = asyncio.create_task(server.serve(sockets=[sock]), name="xiaowei-web")
    stopping = asyncio.Event()
    watch = asyncio.create_task(_watch(runtime.lock, config.lock_check_seconds, stopping))
    stopped = asyncio.create_task(stop.wait())
    waiting: set[asyncio.Task[Any]] = {web, watch, stopped}
    if feishu is not None:
        waiting.add(feishu.consumers)
    done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
    # Uvicorn 也会捕获 SIGINT/SIGTERM：它因信号先于 ``stop`` 正常结束时同样是正常停止。
    signalled = web in done and server.should_exit and _finished_cleanly(web)
    healthy = stopped in done or signalled
    if not healthy:
        logger.error("组件意外结束，开始停止")
    server.should_exit = True
    if feishu is not None:
        healthy = await _stop_feishu(config, feishu) and healthy
    await asyncio.wait({web})
    if not _finished_cleanly(web):
        logger.error("Web 服务异常结束")
        healthy = False
    stopping.set()
    stopped.cancel()
    await asyncio.wait({watch, stopped})
    if not runtime.readiness.ok:
        logger.error("readiness 已锁低（%s），需重启后由启动恢复处理", runtime.readiness.reason)
        healthy = False
    return 0 if healthy else 1


def _finished_cleanly(task: asyncio.Task[Any]) -> bool:
    return not task.cancelled() and task.exception() is None


async def _start_feishu(
    config: ServeConfig,
    runtime: Runtime,
    clock: Callable[[], datetime],
    channel: LarkChannel | None,
    components: dict[str, str],
) -> _Feishu | None:
    """装配飞书；凭据或配置错误使启动失败，长连接连不上只让飞书不可用。"""
    feishu = config.feishu
    if feishu is None:
        return None
    transport = LarkTransport(channel or lark_channel(feishu), feishu)
    gateway = FeishuGateway(runtime.service, feishu, transport.send, clock=clock)
    consumers = asyncio.create_task(gateway.run(), name="xiaowei-feishu")
    try:
        await transport.start(gateway.receive)
    except Exception as exc:
        logger.error("飞书长连接未能启动，飞书不可用：%s", type(exc).__name__)
        components["feishu"] = "unavailable"
    else:
        components["feishu"] = "connected"
    return _Feishu(gateway, transport, consumers)


async def _stop_feishu(config: ServeConfig, feishu: _Feishu) -> bool:
    """drain → 取消消费者 → 关闭长连接；任一步超时或异常返回 False（readiness 已按需锁低）。

    异常只记录类型并转为非零退出码，不中断其余关闭步骤（Web、实例锁与引擎仍要按序关闭）。
    """
    consumers = feishu.consumers
    drained = False
    if not consumers.done():
        drained = await feishu.gateway.drain(config.shutdown_timeout_seconds)
    consumers.cancel()
    await asyncio.wait({consumers})
    if not consumers.cancelled() and consumers.exception() is not None:
        logger.error("飞书消费者异常结束：%s", type(consumers.exception()).__name__)
        drained = False
    try:
        closed = await feishu.transport.stop()
    except Exception as exc:
        logger.error("飞书长连接关闭异常：%s", type(exc).__name__)
        closed = False
    return drained and closed


# ---- 维护命令 ------------------------------------------------------------------------


async def initialize(config: ServeConfig) -> None:
    async with _engine(config) as engine:
        await initialize_storage(engine)


async def upgrade(config: ServeConfig) -> int:
    async with _engine(config) as engine:
        return await upgrade_storage(engine)


async def cleanup(config: ServeConfig, *, batch_size: int) -> CleanupReport:
    async with _engine(config) as engine:
        await check_storage(engine)
        return await cleanup_expired(engine, now=now(), batch_size=batch_size)


class ResendNotConfiguredError(Exception):
    def __init__(self) -> None:
        super().__init__("未配置飞书，没有可重发的渠道")


async def _no_starrocks() -> Connection:
    raise RuntimeError("显式重发不连接 StarRocks")


async def resend(
    config: ServeConfig,
    *,
    subject_id: str,
    chat_id: str,
    message_id: str,
    clock: Callable[[], datetime] = now,
    feishu_channel: LarkChannel | None = None,
) -> SendOutcome | None:
    """显式重发一条飞书结果：只接受当前 owner 下 completed 且投递为 failed/unknown 的记录。

    ``chat_id`` 由操作者给出（G2 未决，目的地不持久化）；它参与会话语境摘要，因此不同的 chat
    定位不到原记录，不能把结果改投到别处。返回 None 表示该记录当前不可重发。
    """
    feishu = config.feishu
    if feishu is None:
        raise ResendNotConfiguredError
    configure_runtime()
    async with _engine(config) as engine, hold_backend(engine) as sender:
        # 发送期间占用一条连接：并发启动的 serve 据此知道这次重发仍在进行，不把它当作遗留发送。
        await check_storage(engine)
        # 重发只复核与发送已保存结果，不调用工具：结构快照从不刷新，StarRocks 从不连接。
        schemas = {
            t.target_id: SchemaCache(
                StarRocksAdapter(t.starrocks, connect=_no_starrocks, clock=clock), clock=clock
            )
            for t in config.targets
        }
        parts = _delivery(config, engine, schemas, Readiness(), clock, sender)
        transport = LarkTransport(feishu_channel or lark_channel(feishu), feishu)

        async def transmit(delivery: Delivery) -> SendOutcome:
            text = render(delivery, feishu.max_reply_chars)
            try:
                return await transport.send(chat_id, text)
            except Exception as exc:
                logger.error("飞书发送异常，按结果不明记录：%s", type(exc).__name__)
                return "unknown"

        ref = RequestRef(
            channel="feishu", subject_id=subject_id, conversation_id=chat_id, request_id=message_id
        )
        try:
            return await parts.results.send(ref, transmit, resend=True)
        finally:
            if not await transport.stop():
                logger.error("飞书发送通道关闭超时")
