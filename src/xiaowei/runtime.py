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
不调用工具，经同一 ``EvidenceStore`` 重验（含按当前 StarRocks 权限与对象版本复核证据依赖：只有
零行探测与元数据读取，不执行原业务 SQL）与投递 CAS 后发送一次。

凭据只以 ``env:NAME`` 引用出现在配置中，在用到它的装配步骤才解析；错误信息不含凭据、连接串或
上游原文。``model_transport``、``starrocks_connect``（按目标 ID）、``feishu_channel`` 只供测试替换
最底层 I/O，治理、证据与状态路径不变。
"""

import asyncio
import ipaddress
import logging
import math
import os
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx2
import openai
import uvicorn
from agents import Agent, MaxTurnsExceeded, Runner, function_tool
from lark_channel.channel.errors import FeishuChannelError, FeishuChannelErrorCode
from lark_channel.ws.exception import ClientException
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

from xiaowei.app import (
    AppConfig,
    Application,
    BusinessContext,
    DataPolicy,
    TargetInfo,
    _causes,
    safe_run_config,
)
from xiaowei.channel import (
    AccessDecision,
    ChannelService,
    GroupScope,
    RequestRef,
    ResultDelivery,
)
from xiaowei.channel_store import ChannelStore, RecoveryReport, SendOutcome
from xiaowei.config import (
    FeishuConfig,
    MCPServerConfig,
    SecretRefError,
    WebConfig,
    configure_runtime,
    is_secret_ref,
    resolve_secret_ref,
)
from xiaowei.evidence import EvidenceStore
from xiaowei.feishu import FeishuGateway, LarkChannel, LarkTransport, lark_channel, send_delivery
from xiaowei.governance import GovernedTools, Projection, ToolCatalog, ToolPolicy
from xiaowei.mcp import MCPIntegration
from xiaowei.model_api import ModelProfile, open_model
from xiaowei.models import (
    AUDIENCES,
    PROMETHEUS_READ_POLICIES,
    AgentAnswer,
    Audience,
    Budget,
    Channel,
    ClusterId,
    Identity,
    Label,
    Owner,
    ToolContract,
    ToolId,
)
from xiaowei.session import CleanupReport, SessionLimits, cleanup_expired
from xiaowei.starrocks import (
    Connector,
    StarRocksAdapter,
    StarRocksTarget,
    open_starrocks,
)
from xiaowei.starrocks_schema import DependencyCheck, SchemaCache
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
    check_digest_key,
    check_storage,
    hold_backend,
    hold_instance_lock,
    initialize_storage,
    open_engine,
    upgrade_storage,
)
from xiaowei.vertex_model import ModelAPIStatusError, ModelAPITransportError
from xiaowei.web import create_web_app

logger = logging.getLogger(__name__)

_CHECKS_PER_LISTED_OBJECT = 5
_RUNTIME_CLOSE_BOUND_SECONDS = 25
_STOP_MARGIN_SECONDS = 5
_SCHEMA_CLOSE_TIMEOUT_SECONDS = 5
_FEISHU_START_CANCEL_TIMEOUT_SECONDS = 5
_FEISHU_CONSUMER_CANCEL_TIMEOUT_SECONDS = 12
_WEB_EXIT_OVERHEAD_SECONDS = 2
_WATCH_STOP_TIMEOUT_SECONDS = 12


class ConfigError(Exception):
    """配置文件不可读或不符合契约；消息只含字段路径与固定说明，不含配置值。"""


class RuntimeCloseError(Exception):
    """运行资源无法在部署停止上界内关闭。"""


class _StartupStoppedError(Exception):
    pass


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
    mcp_servers: tuple[MCPServerConfig, ...] = ()
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

    @field_validator("mcp_servers")
    @classmethod
    def _selected_monitoring_sources(
        cls, value: tuple[MCPServerConfig, ...]
    ) -> tuple[MCPServerConfig, ...]:
        ids = [server.server_id for server in value]
        if len(set(ids)) != len(ids):
            raise ValueError("mcp_servers 中的 server_id 不能重复")
        if any(
            policy_id not in PROMETHEUS_READ_POLICIES or policy_id != f"prometheus.{remote_name}"
            for server in value
            for remote_name, policy_id in server.allowed_tools.items()
        ):
            raise ValueError("只允许已核约的 Prometheus 只读工具及其准确映射")
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
        if {server.server_id for server in self.mcp_servers} & {
            target.target_id for target in self.targets
        }:
            raise ValueError("监控源 ID 不能与 StarRocks 目标 ID 重复")
        if not self.data_policy.model_tools <= registered:
            raise ValueError("data_policy.model_tools 只能包含已登记的工具")
        for tools in self.access.grants.values():
            if not tools <= registered:
                raise ValueError("access.grants 只能授予已登记的工具")
        storage = self.storage
        if storage.request_retention_seconds > storage.evidence_retention_seconds:
            raise ValueError("可重发结果的保留期不得长于证据保留期")
        if self.feishu is not None:
            if self.web.operator_id in self.feishu.users.values():
                raise ValueError("Web 操作者与飞书用户不能使用同一个内部 subject")
            # 空 grant 在 StaticAccess 中等于无权限；已登记却无权限的用户在启动前拒绝，不静默失效。
            for subject in sorted(set(self.feishu.users.values())):
                if not self.access.grants.get(subject):
                    raise ValueError(
                        f"飞书用户 subject {subject} 缺少有效授权（access.grants 非空）"
                    )
            if self.feishu.consumer_count > self.max_concurrent_turns:
                raise ValueError("feishu.consumer_count 不得超过 max_concurrent_turns")
            group = self.feishu.group
            if group is not None and not group.tools <= registered:
                raise ValueError("feishu.group.tools 只能包含已登记的工具")
            if group is not None and group.max_wait_seconds >= storage.request_retention_seconds:
                raise ValueError("feishu.group.max_wait_seconds 必须短于请求保留期")
        # 一轮新列表的每个对象在工具记录、写入 Session、最终校验、提交时的校验与提交前的整段回放
        # 中各复核一次；上限容不下最大一页时，满页的列表必然在提交前因次数用完而失败。
        page = max(t.starrocks.policy.max_rows for t in self.targets)
        if self.budget.max_scope_checks < _CHECKS_PER_LISTED_OBJECT * page:
            raise ValueError(
                "budget.max_scope_checks 至少为最大 policy.max_rows 的 "
                f"{_CHECKS_PER_LISTED_OBJECT} 倍"
            )
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
            f"{_error_path(error['loc'])}: {_reason(error)}"
            for error in exc.errors(include_url=False, include_input=False)
        ]
        raise ConfigError("配置不符合要求：" + "；".join(dict.fromkeys(problems))) from None


# 键本身是身份标识的字典：错误只报告到字典这一层，不把键（飞书 open_id）写进路径。
_IDENTITY_KEYED = frozenset({("feishu", "users")})


def _error_path(loc: tuple[int | str, ...]) -> str:
    for keyed in _IDENTITY_KEYED:
        if loc[: len(keyed)] == keyed:
            loc = keyed
    return ".".join(str(part) for part in loc) or "<root>"


# 发行包模板与 OPERATIONS 示例里列出的固定待填写值：只按完整值识别，不按“尖括号里有中文”之类的
# 形状猜测，真实值里出现尖括号也照常通过。模板增删标记时同步这里（测试逐项核对仓库模板）。
_JSON_TEMPLATE_VALUES = frozenset(
    {
        # Vertex 模型 ID 只能是一个 URL 路径段：标记不用尖括号，才能先通过结构检查、再按占位符报告。
        "replace-with-approved-vertex-model",
        "<集群用途，交给模型选择集群>",
        "<StarRocks FE 地址>",
        "<默认 database>",
        "<只读账号>",
        "<飞书 tenant_key>",
        "<内部 subject>",
        "cli_replacewithappid",
        "oc_replace_with_chat_id",
        # 历史升级标记：C2 之前发行的模板（OpenAI Profile 与第二个目标 archive）和 6d7c6af 的单聊
        # 名单键。当前模板已不含这些值，沿用旧副本未替换时仍拒绝。
        "<获准的模型 ID>",
        "<另一个集群的用途；未配置审计源，不能列慢查询>",
        "<另一个 StarRocks FE 地址>",
        "ou_replace_with_open_id",
    }
)
_ENV_TEMPLATE_VALUES = frozenset(
    {
        "<生成并安全保存的 PostgreSQL 密码>",
        "postgresql+asyncpg://xiaowei:<URI 编码后的密码>@postgres:5432/xiaowei",
        "<生成并安全保存的摘要密钥>",
        "<获准 Model Profile 的密钥>",
        "<warehouse 只读账号密码>",
        "<archive 只读账号密码>",
        "<飞书应用 App Secret>",
    }
)
# Compose 用它初始化 PostgreSQL，并经 env_file 交给小维容器；原生部署使用外部库时不设置。
_POSTGRES_PASSWORD_ENV = "XW_POSTGRES_PASSWORD"  # noqa: S105 - 变量名，不是秘密


def _is_template_value(value: str) -> bool:
    return value in _JSON_TEMPLATE_VALUES


def _template_paths(value: object, path: str) -> list[str]:
    if isinstance(value, Mapping):
        found = [path] if any(_is_template_value(str(key)) for key in value) else []
        keyed = tuple(path.split(".")) in _IDENTITY_KEYED
        for key, item in value.items():
            child = path if keyed else f"{path}.{key}" if path else str(key)
            found += _template_paths(item, child)
        return list(dict.fromkeys(found))
    if isinstance(value, list):
        return [
            found
            for index, item in enumerate(value)
            for found in _template_paths(item, f"{path}.{index}")
        ]
    return [path] if isinstance(value, str) and _is_template_value(value) else []


def validate_placeholders(config: ServeConfig) -> None:
    """拒绝仍是发行模板占位符的 JSON 字段；只报告字段路径，不读环境、不读文件、不联网。

    ``serve`` 与 ``config check`` 在任何外部 I/O 前共用；模板语法上可读，替换后才能运行。
    """
    paths = _template_paths(config.model_dump(mode="json"), "")
    if paths:
        raise ConfigError("以下字段仍是模板占位符，请替换为实际值：" + "、".join(paths))


def _resolve_checked(field: str, ref: str) -> None:
    try:
        value = resolve_secret_ref(ref).get_secret_value()
    except SecretRefError:
        raise ConfigError(f"{field}: 引用的环境变量未设置或为空") from None
    if value in _ENV_TEMPLATE_VALUES:
        raise ConfigError(f"{field}: 引用的环境变量仍是 .env 模板占位符，请替换为实际值")


def _is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def validate_config(
    config: ServeConfig,
    *,
    container_port: int | None = None,
    container_bind_address: str | None = None,
    stop_grace_seconds: float | None = None,
) -> None:
    """离线检查模板占位符、凭据、CA 与可选容器绑定参数；不连接外部服务或写文件。"""
    validate_placeholders(config)
    refs = [
        ("storage.database_url_ref", config.storage.database_url_ref),
        ("storage.digest_key_ref", config.storage.digest_key_ref),
        ("model.api_key_ref", config.model.api_key_ref),
    ]
    refs.extend(
        (f"targets.{index}.starrocks.password_ref", target.starrocks.password_ref)
        for index, target in enumerate(config.targets)
    )
    refs.extend(
        (f"mcp_servers.{index}.auth_ref", server.auth_ref)
        for index, server in enumerate(config.mcp_servers)
        if server.auth_ref is not None
    )
    if config.feishu is not None:
        refs.append(("feishu.app_secret_ref", config.feishu.app_secret_ref))
    for field, ref in refs:
        _resolve_checked(field, ref)
    if os.environ.get(_POSTGRES_PASSWORD_ENV) in _ENV_TEMPLATE_VALUES:
        raise ConfigError(f"{_POSTGRES_PASSWORD_ENV}: 仍是 .env 模板占位符，请替换为实际值")

    for index, target in enumerate(config.targets):
        ca_file = target.starrocks.tls_ca_file
        if ca_file is None:
            continue
        try:
            with Path(ca_file).open("rb") as stream:
                stream.read(1)
        except OSError:
            raise ConfigError(f"targets.{index}.starrocks.tls_ca_file: CA 文件不可读") from None

    if container_port is not None:
        if not 1 <= container_port <= 65535 or container_port != config.listen_port:
            raise ConfigError("listen_port: 与容器发布端口不一致")
        if container_bind_address is not None and not _is_ip_literal(container_bind_address):
            raise ConfigError("XW_WEB_BIND_ADDRESS: 必须是 IP 地址，例如 0.0.0.0 或 127.0.0.1")
    if stop_grace_seconds is not None:
        if stop_grace_seconds < minimum_stop_grace_seconds(config):
            raise ConfigError("shutdown_timeout_seconds: 容器停止宽限不足")


def validate_model_config(config: ServeConfig) -> None:
    """``model check`` 的离线预检：只解析活动 Profile 的模型凭据，不读其他秘密、CA 或部署参数。"""
    _resolve_checked("model.api_key_ref", config.model.api_key_ref)


# ---- model check ---------------------------------------------------------------------

_CHECK_TOOL = "model_check_lookup"
_CHECK_MAX_TURNS = 3  # 调用工具、收到结果后作答只需 2 步；多出的一步用于识别重复调用
_CHECK_INSTRUCTIONS = (
    "这是模型配置检查。先调用一次 model_check_lookup 工具（region 填 east），只调用一次；"
    "然后给出最终回答：evidence_ids 为空列表，inferences 为空列表，clarification 为 null，"
    "advice 用一句话说明工具返回的数值。"
)
_CHECK_INPUT = "请完成模型配置检查。"

CheckReason = Literal[
    "auth_failed",
    "rate_limited",
    "upstream_error",
    "unreachable",
    "model_failed",
    "tool_not_called",
    "tool_repeated",
    "answer_invalid",
]


class ModelCheckError(Exception):
    """模型检查未通过；只携带固定类别，不含提示、模型正文、工具结果或上游原文。"""

    def __init__(self, reason: CheckReason) -> None:
        super().__init__(reason)
        self.reason: CheckReason = reason


async def check_model(
    config: ServeConfig, *, transport: httpx2.AsyncBaseTransport | None = None
) -> None:
    """用固定合成指令与一个进程内无 I/O 工具，经与 ``serve`` 相同的模型装配完成一次工具往返。

    不连接 PostgreSQL、StarRocks 或飞书，不写 Session、请求或 Evidence；会产生真实模型请求与用量。
    最终回答必须是无证据的 ``advice`` 分支：本命令没有 Evidence，所以不运行 Evidence 校验，也不接受
    形状正确但混用分支的回答。模型客户端打开或关闭失败同样只报告固定类别（``model_failed``），
    不报告为通过。
    """
    configure_runtime()
    calls = 0

    @function_tool(name_override=_CHECK_TOOL)
    def lookup(region: str) -> dict[str, object]:
        """返回固定的合成数值。"""
        nonlocal calls
        calls += 1
        return {"value": 42}

    # 分类覆盖模型客户端的进入、运行与关闭：任何阶段的失败都只报告固定类别。
    try:
        async with open_model(config.model, transport=transport) as binding:
            agent = Agent[None](
                name="xiaowei-model-check",
                instructions=_CHECK_INSTRUCTIONS,
                model=binding.model,
                model_settings=binding.settings,
                tools=[lookup],
                output_type=AgentAnswer,
            )
            result = await Runner.run(
                agent, _CHECK_INPUT, max_turns=_CHECK_MAX_TURNS, run_config=safe_run_config()
            )
            _check_result(calls, result.final_output)
    except ModelCheckError:
        raise
    except Exception as exc:
        raise ModelCheckError(_check_reason(exc)) from None


def _check_result(calls: int, answer: object) -> None:
    if calls == 0:
        raise ModelCheckError("tool_not_called")
    if calls > 1:
        raise ModelCheckError("tool_repeated")
    if not (
        isinstance(answer, AgentAnswer)
        and answer.evidence_ids == ()
        and answer.inferences == []
        and answer.clarification is None
        and answer.advice
    ):
        raise ModelCheckError("answer_invalid")


def _check_reason(exc: Exception) -> CheckReason:
    if isinstance(exc, MaxTurnsExceeded):
        return "tool_repeated"
    for cause in _causes(exc):
        if isinstance(cause, (ModelAPIStatusError, openai.APIStatusError)):
            status = cause.status_code
            if status in (401, 403):
                return "auth_failed"
            return "rate_limited" if status == 429 else "upstream_error"
        if isinstance(cause, (ModelAPITransportError, openai.APIConnectionError, TimeoutError)):
            return "unreachable"
    return "model_failed"


def stop_upper_bound_seconds(config: ServeConfig) -> int:
    """当前停止顺序的保守上界，不含要求操作者额外保留的安全余量。"""
    feishu_stop = 0 if config.feishu is None else math.ceil(config.feishu.stop_timeout_seconds)
    consumer_cancel = 0 if config.feishu is None else _FEISHU_CONSUMER_CANCEL_TIMEOUT_SECONDS
    channel_and_web = math.ceil(config.shutdown_timeout_seconds) + max(
        feishu_stop, _WEB_EXIT_OVERHEAD_SECONDS
    )
    return (
        channel_and_web
        + consumer_cancel
        + _WATCH_STOP_TIMEOUT_SECONDS
        + _RUNTIME_CLOSE_BOUND_SECONDS
    )


def minimum_stop_grace_seconds(config: ServeConfig) -> int:
    return stop_upper_bound_seconds(config) + _STOP_MARGIN_SECONDS


def _reason(error: Mapping[str, object]) -> str:
    """Pydantic 内置错误的说明不含输入值；自定义校验只给固定文字。"""
    if error.get("type") == "value_error":
        context = error.get("ctx")
        cause = context.get("error") if isinstance(context, Mapping) else None
        return str(cause) if cause is not None else "取值不符合要求"
    return str(error.get("msg", "取值不符合要求"))


def _registered_tools(config: ServeConfig) -> frozenset[str]:
    """装配时会登记的工具；监控只登记可信配置选中的已核约工具。"""
    audited = any(t.starrocks.audit is not None for t in config.targets)
    monitoring = {
        server.tool_id(name) for server in config.mcp_servers for name in server.allowed_tools
    }
    return QUERY_TOOLS | (AUDIT_TOOLS if audited else frozenset()) | monitoring


class _PrometheusQueryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str


class _PrometheusQueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    result: str
    warnings: list[str] | None
    # 远端不返回表达式；MCPIntegration 从已治理的实际请求补入该字段。
    query: str | None = None


class _PrometheusWindowArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start_time: str
    end_time: str


class _PrometheusRangeArgs(_PrometheusWindowArgs):
    query: str
    step: str


class _PrometheusLabelsArgs(_PrometheusWindowArgs):
    matches: list[str]


class _PrometheusLabelValuesArgs(_PrometheusLabelsArgs):
    label: str


class _PrometheusMetadataArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    metric: str


class _PrometheusRulesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _PrometheusWindowResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    result: str
    warnings: list[str] | None
    # 实际网络参数由可信执行准备补入，远端同名字段不能冒充它们。
    start_time: str | None = None
    end_time: str | None = None


class _PrometheusRangeResult(_PrometheusWindowResult):
    query: str | None = None
    step: str | None = None


class _MetricMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: str
    help: str
    unit: str


class _PrometheusMetadataResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    metadata: dict[str, list[_MetricMetadata]]


class _LoadedRule(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    query: str
    duration: float | None = Field(default=None, description="告警 for 持续秒数；未返回时为 null")
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] | None = None
    state: str | None = None
    health: str
    last_evaluation: str = Field(alias="lastEvaluation")


class _RuleGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    interval: float
    rules: list[_LoadedRule]


class _PrometheusRulesResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    groups: list[_RuleGroup]


def _monitoring_catalog(
    config: ServeConfig,
) -> tuple[tuple[ToolContract, ...], tuple[ToolPolicy, ...]]:
    if not config.mcp_servers:
        return (), ()
    query_policy = ToolPolicy(
        policy_id="prometheus.query",
        arguments=_PrometheusQueryArgs,
        result=_PrometheusQueryResult,
        projections={
            audience: Projection(
                fields=("query", "result", "warnings"),
                max_bytes=config.projection_bytes[audience],
            )
            for audience in AUDIENCES
        },
        required=("query", "result", "warnings"),
        fact_note="Prometheus MCP 返回的结果为服务端排版文本；采集时间不是指标求值时间。",
    )
    windows = (
        "时间为 Unix 秒、含时区 RFC3339，或 now/-15m/-1h/-7d；窗宽最多31天。"
        "自主选择时间范围，不必先发现或读规则。"
    )
    specifications = (
        (
            "range_query",
            _PrometheusRangeArgs,
            _PrometheusRangeResult,
            ("query", "start_time", "end_time", "step", "result", "warnings"),
            "只读 PromQL 范围查询；step 为秒或 30s/5m 等单单位时长，至少1秒，最多11000点/序列。"
            + windows,
            "结果为服务端排版文本；求值网格以实际起止秒和步长为准，不代表未查范围。",
        ),
        (
            "label_names",
            _PrometheusLabelsArgs,
            _PrometheusWindowResult,
            ("start_time", "end_time", "result", "warnings"),
            "发现标签名称；matches 为选择器列表，空列表表示源内发现。" + windows,
            "结果是该窗口发现的标签名称，不是实时健康检查。",
        ),
        (
            "label_values",
            _PrometheusLabelValuesArgs,
            _PrometheusWindowResult,
            ("start_time", "end_time", "result", "warnings"),
            "发现指定 label 的值；__name__ 可发现指标，instance/nodename 可寻找主机。"
            "matches 空列表表示源内发现。" + windows,
            "标签值只证明窗口内存在相应序列，不证明当前主机健康或配置。",
        ),
        (
            "series",
            _PrometheusLabelsArgs,
            _PrometheusWindowResult,
            ("start_time", "end_time", "result", "warnings"),
            "按至少一个选择器读取序列标签集，可定位主机；不返回指标数值。" + windows,
            "标签集为发现事实；主机配置仅限实际采集的标签/指标，不是资产清单。",
        ),
        (
            "metric_metadata",
            _PrometheusMetadataArgs,
            _PrometheusMetadataResult,
            ("metadata",),
            "读取指标的类型/help/unit；metric 空串可发现全部元数据。"
            "官方 Server 不传回上游 warnings，结果不证明查询完整性或健康。",
            "仅指标元数据；官方 Server 不传回上游 warnings，不能据此判定健康或完整。",
        ),
        (
            "list_rules",
            _PrometheusRulesArgs,
            _PrometheusRulesResult,
            ("groups",),
            "读取当前已加载的原生规则和求值状态，不修改规则。"
            "duration 是告警 for 持续秒数，null 表示上游未返回。"
            "health 是规则求值状态；官方 Server 不返回 type 和上游 warnings。",
            "duration 为告警 for 持续秒数，null 表示未返回；规则定义/求值状态不是主机健康。"
            "官方 Server 不返回 type 和上游 warnings。",
        ),
    )
    policies = {"query": query_policy}
    descriptions = {"query": "只读 PromQL 即时查询；由 Server 即时求值，不代表历史窗口。"}
    for name, arguments, result, fields, description, note in specifications:
        policies[name] = ToolPolicy(
            policy_id=f"prometheus.{name}",
            arguments=arguments,
            result=result,
            projections={
                audience: Projection(fields=fields, max_bytes=config.projection_bytes[audience])
                for audience in AUDIENCES
            },
            required=fields,
            fact_note=note,
        )
        descriptions[name] = description
    selected = {name for server in config.mcp_servers for name in server.allowed_tools}
    contracts = tuple(
        ToolContract(
            tool_id=server.tool_id(name),
            target_id=server.server_id,
            input_schema=policies[name].arguments.model_json_schema(),
            policy_id=policies[name].policy_id,
            description=f"监控源 {server.server_id}：{descriptions[name]}",
        )
        for server in config.mcp_servers
        for name in server.allowed_tools
    )
    return contracts, tuple(policy for name, policy in policies.items() if name in selected)


def _app_config(config: ServeConfig) -> AppConfig:
    monitoring = frozenset(
        server.tool_id(name) for server in config.mcp_servers for name in server.allowed_tools
    )
    audit = _registered_tools(config) - QUERY_TOOLS - monitoring
    return AppConfig(
        purposes={
            "query": QUERY_TOOLS | audit | monitoring,
            "diagnose": DIAGNOSE_TOOLS | audit,
        },
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


MemberCheck = Callable[[str, str], Awaitable[bool]]
"""``(chat_id, open_id) -> 是否当前成员``：在取到的名单（可能因页数上限只是部分名单）中找到
发送者才返回 True，这是本次成员证据；未找到只表示无法确认、不证明其不在群，与失败、超时
一样不能返回 True（调用方把异常也当作拒绝）。"""


@dataclass(frozen=True)
class GroupAccess:
    """指定群的共享策略：全员同权，可用全部已配置目标与 ``tools``；成员资格只来自 ``members``。"""

    scope: GroupScope
    tools: frozenset[str]
    members: MemberCheck


class StaticAccess:
    """配置中的授权表：入口解析（``resolve`` / ``resolve_group``）与 Evidence 授权
    （``authorize``）读同一份表。

    个人 subject 与渠道无关：Web 操作者与飞书用户由各自适配器映射为内部 subject，配置校验保证
    二者不重名。获准用户得到全部已配置目标；某目标没有登记的工具由工具目录在调用前拒绝。
    指定群（可选）另有一份共享策略：群内发起人不需要也不使用个人授权，群授权也不进入个人路径。
    """

    def __init__(
        self,
        config: AccessConfig,
        target_ids: frozenset[str],
        group: GroupAccess | None = None,
    ) -> None:
        self._grants: Mapping[str, frozenset[str]] = dict(config.grants)
        self._version = config.policy_version
        self._targets = target_ids
        self._group = group

    async def resolve(self, channel: Channel, subject_id: str) -> AccessDecision | None:
        tools = self._grants.get(subject_id)
        if not tools:
            return None
        return AccessDecision(
            subject_id=subject_id,
            owner=Owner(kind="personal", id=subject_id),
            target_ids=self._targets,
            authorized_tools=tools,
            policy_version=self._version,
        )

    async def resolve_group(
        self, group: GroupScope, subject_id: str, *, verify_member: bool
    ) -> AccessDecision | None:
        shared = self._group
        if shared is None or group != shared.scope or not shared.tools:
            return None
        if verify_member and await shared.members(group.chat_id, subject_id) is not True:
            return None
        return AccessDecision(
            subject_id=subject_id,
            owner=group.owner,
            target_ids=self._targets,
            authorized_tools=shared.tools,
            policy_version=self._version,
        )

    async def authorize(self, identity: Identity, target_id: str, tool_id: str) -> bool:
        """同一轮内的工具与证据复核：不查成员目录，沿用本轮开始时已确认的成员资格。"""
        if target_id not in self._targets:
            return False
        owner = identity.owner
        if owner.kind == "group":
            shared = self._group
            return shared is not None and owner == shared.scope.owner and tool_id in shared.tools
        return owner.id == identity.subject_id and tool_id in self._grants.get(
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
    mcp: MCPIntegration | None = None


@dataclass(frozen=True)
class _Delivery:
    store: ChannelStore
    evidence: EvidenceStore
    results: ResultDelivery
    tools: StarRocksTools


def _group_access(config: ServeConfig, members: MemberCheck | None) -> GroupAccess | None:
    """配置了指定群且有成员目录时的群共享策略；缺少成员目录时群授权一律不成立。"""
    feishu = config.feishu
    if feishu is None or feishu.group is None or members is None:
        return None
    scope = GroupScope(
        app_id=feishu.app_id, tenant_key=feishu.tenant_key, chat_id=feishu.group.chat_id
    )
    return GroupAccess(scope=scope, tools=feishu.group.tools, members=members)


def _delivery(
    config: ServeConfig,
    engine: AsyncEngine,
    schemas: Mapping[str, SchemaCache],
    readiness: Readiness,
    clock: Callable[[], datetime],
    sender: Backend | None = None,
    members: MemberCheck | None = None,
) -> _Delivery:
    """请求存储、工具目录、唯一授权来源、Evidence 与交付；serve 与显式重发共用同一装配。

    ``sender`` 只由显式重发传入：它占用的连接身份记入投递尝试（见 ``ChannelStore``）。
    ``members`` 是指定群的成员目录（飞书 SDK 的成员查询），只在配置了群时使用。
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
    monitoring_contracts, monitoring_policies = _monitoring_catalog(config)
    targets = frozenset(schemas) | {server.server_id for server in config.mcp_servers}
    access = StaticAccess(config.access, frozenset(targets), _group_access(config, members))
    evidence = EvidenceStore(
        engine,
        ToolCatalog(
            (*tools.contracts, *monitoring_contracts), (*tools.policies, *monitoring_policies)
        ),
        authorize=access.authorize,
        clock=clock,
        retention_seconds=storage.evidence_retention_seconds,
        verify_dependencies=DependencyCheck({t: s.adapter for t, s in schemas.items()}),
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
        try:
            async with asyncio.timeout(_SCHEMA_CLOSE_TIMEOUT_SECONDS):
                # 启动中途被取消时，共享的刷新任务不随等待者取消：同样在这里取消并等待。
                for task in loops:
                    task.cancel()
                if loops:
                    await asyncio.wait(loops)
                await asyncio.gather(*(schema.aclose() for schema in schemas.values()))
        except TimeoutError:
            raise RuntimeCloseError("结构刷新关闭超时") from None


@asynccontextmanager
async def open_runtime(
    config: ServeConfig,
    *,
    clock: Callable[[], datetime] = now,
    model_transport: httpx2.AsyncBaseTransport | None = None,
    starrocks_connect: Mapping[str, Connector] | None = None,
    members: MemberCheck | None = None,
) -> AsyncIterator[Runtime]:
    """按唯一顺序装配；退出或任一步失败时逆序关闭并释放实例锁。

    ``members`` 是指定群的成员目录；未给出时群授权一律不成立（群请求开始与交付前都被拒绝）。
    """
    configure_runtime()
    readiness = Readiness()
    async with AsyncExitStack() as stack:
        engine = await stack.enter_async_context(_engine(config))
        lock = await stack.enter_async_context(hold_instance_lock(engine, readiness))
        await check_storage(engine)
        await check_digest_key(engine, resolve_secret_ref(config.storage.digest_key_ref))
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
        parts = _delivery(config, engine, schemas, readiness, clock, members=members)
        recovery = await parts.store.recover(lock)
        logger.info(
            "启动恢复：interrupted=%d unknown=%d unsent=%d",
            recovery.interrupted,
            recovery.unknown,
            recovery.unsent,
        )
        await stack.enter_async_context(_refreshing(schemas))
        model = await stack.enter_async_context(open_model(config.model, transport=model_transport))
        governance = GovernedTools(parts.evidence)
        mcp = (
            await stack.enter_async_context(
                MCPIntegration(config.mcp_servers, governance, clock=clock)
            )
            if config.mcp_servers
            else None
        )
        app = Application(
            _app_config(config),
            model=model,
            engine=engine,
            governance=governance,
            local_tools=parts.tools.executes,
            mcp=mcp,
            clock=clock,
        )
        service = ChannelService(app, parts.results)
        yield Runtime(engine, lock, readiness, recovery, service, mcp)


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
    """运行到 ``stop`` 被设置、持锁连接丢失或某个组件意外结束；返回进程退出码（0 为正常停止）。

    配置了飞书时先装配 SDK 通道（凭据错误使启动失败）：指定群的成员目录经它查询，授权来源因此
    在运行对象装配前就绑定到这一个通道。
    """
    return await _serve_bound(
        config,
        bind_host=config.listen_host,
        stop=stop,
        clock=clock,
        model_transport=model_transport,
        starrocks_connect=starrocks_connect,
        feishu_channel=feishu_channel,
    )


async def _container_serve(
    config: ServeConfig,
    *,
    stop: asyncio.Event,
    clock: Callable[[], datetime] = now,
    model_transport: httpx2.AsyncBaseTransport | None = None,
    starrocks_connect: Mapping[str, Connector] | None = None,
    feishu_channel: LarkChannel | None = None,
) -> int:
    """仅供镜像固定入口调用；JSON 与公开 CLI 都不能选择容器监听模式。"""
    return await _serve_bound(
        config,
        bind_host="0.0.0.0",  # noqa: S104 - 容器网络内监听；宿主发布地址由 XW_WEB_BIND_ADDRESS 决定
        stop=stop,
        clock=clock,
        model_transport=model_transport,
        starrocks_connect=starrocks_connect,
        feishu_channel=feishu_channel,
    )


async def _serve_bound(
    config: ServeConfig,
    *,
    bind_host: str,
    stop: asyncio.Event,
    clock: Callable[[], datetime],
    model_transport: httpx2.AsyncBaseTransport | None,
    starrocks_connect: Mapping[str, Connector] | None,
    feishu_channel: LarkChannel | None,
) -> int:
    sock = _bind(bind_host, config.listen_port)
    try:
        transport = None
        if config.feishu is not None:
            transport = LarkTransport(feishu_channel or lark_channel(config.feishu), config.feishu)
        context = open_runtime(
            config,
            clock=clock,
            model_transport=model_transport,
            starrocks_connect=starrocks_connect,
            members=None if transport is None else transport.is_member,
        )
        try:
            async with _open_runtime_until_stop(context, stop) as runtime:
                return await _serve(config, runtime, sock, stop, clock, transport)
        except _StartupStoppedError:
            return 0
    finally:
        sock.close()


@asynccontextmanager
async def _open_runtime_until_stop(
    context: AbstractAsyncContextManager[Runtime], stop: asyncio.Event
) -> AsyncIterator[Runtime]:
    """装配期间响应停止信号；取消后等逆序清理完成，再报告为正常停止。"""
    opening = asyncio.create_task(context.__aenter__(), name="xiaowei-runtime-open")
    stopping = asyncio.create_task(stop.wait(), name="xiaowei-runtime-startup-stop")
    entered = False
    try:
        done, _ = await asyncio.wait({opening, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if opening not in done:
            opening.cancel()
            try:
                async with asyncio.timeout(_RUNTIME_CLOSE_BOUND_SECONDS + _STOP_MARGIN_SECONDS):
                    await opening
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                raise RuntimeCloseError("启动取消后的资源关闭超时") from None
            raise _StartupStoppedError
        runtime = opening.result()
        entered = True
        if stop.is_set():
            raise _StartupStoppedError
        yield runtime
    finally:
        stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)
        if entered:
            try:
                async with asyncio.timeout(_RUNTIME_CLOSE_BOUND_SECONDS + _STOP_MARGIN_SECONDS):
                    await context.__aexit__(*sys.exc_info())
            except TimeoutError:
                raise RuntimeCloseError("运行资源关闭超时") from None


async def _serve(
    config: ServeConfig,
    runtime: Runtime,
    sock: socket.socket,
    stop: asyncio.Event,
    clock: Callable[[], datetime],
    transport: LarkTransport | None,
) -> int:
    components: dict[str, str] = {"feishu": "disabled"}
    feishu = await _start_feishu(config, runtime, clock, transport, components, stop)
    app = create_web_app(
        runtime.service,
        config.web,
        components=lambda: {
            **components,
            **(
                {f"mcp.{source}": status for source, status in runtime.mcp.source_status.items()}
                if runtime.mcp is not None
                else {}
            ),
        },
    )
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
    done, pending = await asyncio.wait(
        {web}, timeout=math.ceil(config.shutdown_timeout_seconds) + _WEB_EXIT_OVERHEAD_SECONDS
    )
    if pending:
        web.cancel()
        await asyncio.wait({web})
        logger.error("Web 服务关闭超时")
        healthy = False
    if not _finished_cleanly(web):
        logger.error("Web 服务异常结束")
        healthy = False
    stopping.set()
    stopped.cancel()
    try:
        async with asyncio.timeout(_WATCH_STOP_TIMEOUT_SECONDS):
            await asyncio.wait({watch, stopped})
    except TimeoutError:
        logger.error("实例锁监视任务关闭超时")
        healthy = False
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
    transport: LarkTransport | None,
    components: dict[str, str],
    stop: asyncio.Event,
) -> _Feishu | None:
    """启动飞书；长连接连不上只让飞书不可用（群入口同时因机器人身份未解析而拒绝群消息）。"""
    feishu = config.feishu
    if feishu is None or transport is None:
        return None
    gateway = FeishuGateway(
        runtime.service, feishu, transport.send, clock=clock, bot_open_id=transport.bot_open_id
    )
    consumers = asyncio.create_task(gateway.run(), name="xiaowei-feishu")
    starting = asyncio.create_task(transport.start(gateway.receive), name="xiaowei-feishu-start")
    stopping = asyncio.create_task(stop.wait(), name="xiaowei-feishu-start-stop")
    try:
        done, _ = await asyncio.wait({starting, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if starting not in done:
            starting.cancel()
            try:
                async with asyncio.timeout(_FEISHU_START_CANCEL_TIMEOUT_SECONDS):
                    await starting
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                raise RuntimeCloseError("飞书启动取消超时") from None
            partial = _Feishu(gateway, transport, consumers)
            if not await _stop_feishu(config, partial):
                raise RuntimeCloseError("飞书启动取消后的关闭失败")
            raise _StartupStoppedError
        starting.result()
    except (_StartupStoppedError, RuntimeCloseError):
        raise
    except Exception as exc:
        detail = ""
        if isinstance(exc, FeishuChannelError) and isinstance(exc.code, FeishuChannelErrorCode):
            detail = f" error_code={exc.code.value}"
            cause = exc.__cause__
            if isinstance(cause, ClientException) and type(cause.code) is int:
                detail += f" client_code={cause.code}"
        logger.error("飞书长连接未能启动，飞书不可用：%s%s", type(exc).__name__, detail)
        components["feishu"] = "unavailable"
    else:
        components["feishu"] = "connected"
    finally:
        stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)
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
    done, _ = await asyncio.wait({consumers}, timeout=_FEISHU_CONSUMER_CANCEL_TIMEOUT_SECONDS)
    if not done:
        logger.error("飞书消费者取消超时")
        drained = False
    elif not consumers.cancelled() and consumers.exception() is not None:
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
        await initialize_storage(
            engine, digest_key=resolve_secret_ref(config.storage.digest_key_ref)
        )


async def upgrade(config: ServeConfig, *, bind_existing_digest_key: bool = False) -> int:
    async with _engine(config) as engine:
        return await upgrade_storage(
            engine,
            digest_key=resolve_secret_ref(config.storage.digest_key_ref),
            bind_existing_digest_key=bind_existing_digest_key,
        )


async def cleanup(config: ServeConfig, *, batch_size: int) -> CleanupReport:
    async with _engine(config) as engine:
        await check_storage(engine)
        await check_digest_key(engine, resolve_secret_ref(config.storage.digest_key_ref))
        return await cleanup_expired(engine, now=now(), batch_size=batch_size)


class ResendNotConfiguredError(Exception):
    def __init__(self) -> None:
        super().__init__("未配置飞书，没有可重发的渠道")


class ResendTargetError(Exception):
    def __init__(self) -> None:
        super().__init__("单聊重发需要 chat_id；群重发不接受 chat_id，且须已配置指定群")


async def resend(
    config: ServeConfig,
    *,
    subject_id: str,
    message_id: str,
    chat_id: str | None = None,
    group: bool = False,
    clock: Callable[[], datetime] = now,
    feishu_channel: LarkChannel | None = None,
    starrocks_connect: Mapping[str, Connector] | None = None,
) -> SendOutcome | None:
    """显式重发一条飞书结果：只接受当前 owner 下 completed 且投递为 failed/unknown 的记录。

    单聊：``chat_id`` 由操作者给出（目的地不持久化）；它参与会话语境摘要，因此不同的 chat 定位
    不到原记录，不能把结果改投到别处。群（``group=True``）：``subject_id`` 是原发起人的
    ``open_id``，群取自配置，只回复接受时保存的原消息；发送前经 SDK 成员目录重新确认发起人
    当前仍是群成员，不能确认时拒绝。返回 None 表示该记录当前不可重发。
    """
    feishu = config.feishu
    if feishu is None:
        raise ResendNotConfiguredError
    if group != (chat_id is None) or (group and feishu.group is None):
        raise ResendTargetError
    configure_runtime()
    async with _engine(config) as engine, hold_backend(engine) as sender:
        # 发送期间占用一条连接：并发启动的 serve 据此知道这次重发仍在进行，不把它当作遗留发送。
        await check_storage(engine)
        await check_digest_key(engine, resolve_secret_ref(config.storage.digest_key_ref))
        # 重发只复核与发送已保存结果，不调用工具：结构快照从不刷新；StarRocks 只用于复核证据
        # 依赖（零行探测与元数据读取），连接按需建立、用完即关。
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
        transport = LarkTransport(feishu_channel or lark_channel(feishu), feishu)
        try:
            return await _resend(
                config, engine, schemas, clock, sender, transport, subject_id, message_id, chat_id
            )
        finally:
            if not await transport.stop():
                logger.error("飞书发送通道关闭超时")


async def _resend(
    config: ServeConfig,
    engine: AsyncEngine,
    schemas: Mapping[str, SchemaCache],
    clock: Callable[[], datetime],
    sender: Backend,
    transport: LarkTransport,
    subject_id: str,
    message_id: str,
    chat_id: str | None,
) -> SendOutcome | None:
    """单聊发往操作者给出的 ``chat_id``；群（``chat_id`` 为 None）只回复配置群中的原消息。"""
    feishu = config.feishu
    if feishu is None:
        raise ResendNotConfiguredError
    parts = _delivery(
        config, engine, schemas, Readiness(), clock, sender, members=transport.is_member
    )
    group = _group_access(config, transport.is_member) if chat_id is None else None
    if group is not None:
        conversation, reply_to = group.scope.chat_id, message_id
    elif chat_id is not None:
        conversation, reply_to = chat_id, None
    else:
        raise ResendTargetError

    transmit = partial(
        send_delivery,
        send=transport.send,
        chat_id=conversation,
        max_chars=feishu.max_reply_chars,
        reply_to=reply_to,
    )

    ref = RequestRef(
        channel="feishu",
        subject_id=subject_id,
        conversation_id=conversation,
        request_id=message_id,
        group=None if group is None else group.scope,
    )
    return await parts.results.send(ref, transmit, resend=True)
