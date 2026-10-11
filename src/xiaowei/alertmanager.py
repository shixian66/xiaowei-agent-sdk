"""外部 Alertmanager API v2 Adapter：固定读取与群批准的创建/提前结束静默。

API 返回完整的过滤结果；分页在有界接收后进行，不能用 limit 限制上游负荷。
对完整响应做白名单投影，不保存或向模型、Session、渠道暴露通知目的地、
原配置、作者或 peer 地址。
"""

# ruff: noqa: N815 - 事实字段保留官方 API v2 的名称，避免混淆抑制类别。

import asyncio
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx2
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from xiaowei.actions import ActionBinding, ActionPlan, proposal_policy
from xiaowei.config import is_secret_ref, resolve_secret_ref
from xiaowei.evidence import arguments_digest
from xiaowei.governance import (
    Execute,
    MonitoringReadError,
    Prechecked,
    Projection,
    ToolCatalog,
    ToolPolicy,
    ToolRejectedError,
)
from xiaowei.models import AUDIENCES, Audience, ToolContract, ToolObservation, ToolRequest

ReadTool = Literal[
    "get_alerts", "get_alert_groups", "get_silences", "get_silence", "get_receivers", "get_status"
]
WriteAction = Literal["create_silence", "cancel_silence"]


class AlertmanagerConfig(BaseModel):
    """可信根地址及 Basic 安全引用；无认证仅用于已有网络限制的只读源。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    server_id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]{1,32}$")]
    url: str
    username_ref: str | None
    password_ref: str | None
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_response_bytes: int = Field(gt=0)
    tools: tuple[ReadTool, ...] = Field(min_length=1)
    write_actions: tuple[WriteAction, ...] = ()

    @field_validator("server_id")
    @classmethod
    def _not_local(cls, value: str) -> str:
        if value == "local":
            raise ValueError("server_id 不能使用本地工具命名空间 local")
        return value

    @field_validator("url")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Alertmanager 根地址须为不含用户信息、查询与片段的 HTTP(S) 地址")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _references(self) -> "AlertmanagerConfig":
        if (self.username_ref is None) != (self.password_ref is None):
            raise ValueError("Basic 的 username_ref/password_ref 必须成对配置或均为空")
        if any(
            ref is not None and not is_secret_ref(ref)
            for ref in (self.username_ref, self.password_ref)
        ):
            raise ValueError("Basic 凭据必须使用 env:NAME 安全引用")
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("Alertmanager tools 不能重复")
        if len(set(self.write_actions)) != len(self.write_actions):
            raise ValueError("Alertmanager write_actions 不能重复")
        if self.write_actions and self.username_ref is None:
            raise ValueError("Alertmanager 写动作必须配置 Basic 安全引用")
        return self

    def tool_id(self, name: str) -> str:
        return f"{self.server_id}/{name}"

    @property
    def action_tools(self) -> frozenset[str]:
        return frozenset(
            self.tool_id(name)
            for action in self.write_actions
            for name in (action, f"propose_{action}")
        )


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")


class _Args(_Model):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class _PageArgs(_Args):
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class _FilterArgs(_PageArgs):
    filters: list[Annotated[str, StringConstraints(min_length=1, max_length=8192)]] = Field(
        max_length=32
    )

    @field_validator("filters")
    @classmethod
    def _nonblank(cls, value: list[str]) -> list[str]:
        if any(not matcher.strip() for matcher in value):
            raise ValueError("匹配器不能为空白")
        return value


class _AlertArgs(_FilterArgs):
    active: bool
    silenced: bool
    inhibited: bool
    unprocessed: bool
    receiver: str = Field(max_length=256)


class _GroupArgs(_FilterArgs):
    active: bool
    silenced: bool
    inhibited: bool
    muted: bool
    receiver: str = Field(max_length=256)


def _uuid(value: str) -> str:
    return str(UUID(value))


class _SilenceArgs(_Args):
    silence_id: Annotated[str, AfterValidator(_uuid)]


def _timestamp(value: str) -> str:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("时间须包含时区")
    return instant.astimezone(UTC).isoformat()


_Time = Annotated[str, AfterValidator(_timestamp)]


def _write_time(value: str) -> str:
    instant = datetime.fromisoformat(_timestamp(value))
    return instant.replace(microsecond=instant.microsecond // 1000 * 1000).isoformat()


class _WriteMatcher(_Args):
    name: str = Field(min_length=1, max_length=256)
    value: str = Field(max_length=8192)
    isRegex: bool
    isEqual: bool


class _CreateSilenceArgs(_Args):
    matchers: list[_WriteMatcher] = Field(min_length=1, max_length=32)
    starts_at: Annotated[str, AfterValidator(_write_time)]
    ends_at: Annotated[str, AfterValidator(_write_time)]
    comment: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def _ordered(self) -> "_CreateSilenceArgs":
        if datetime.fromisoformat(self.starts_at) >= datetime.fromisoformat(self.ends_at):
            raise ValueError("结束时间必须晚于开始时间")
        return self


class _ResponseLimitError(ValueError):
    """只供当前匹配影响准备识别容量不足；普通读取/写回读仍失败关闭。"""


class _Receiver(_Model):
    name: str


class _AlertStatus(_Model):
    state: Literal["unprocessed", "active", "suppressed"]
    silencedBy: list[str]
    inhibitedBy: list[str]
    mutedBy: list[str]


class _Alert(_Model):
    labels: dict[str, str]
    annotations: dict[str, str]
    fingerprint: str
    startsAt: _Time
    endsAt: _Time
    updatedAt: _Time
    status: _AlertStatus
    receivers: list[_Receiver]

    @field_validator("annotations")
    @classmethod
    def _notes(cls, value: dict[str, str]) -> dict[str, str]:
        return {name: text for name, text in value.items() if name in {"summary", "description"}}


class _Matcher(_Model):
    name: str
    value: str
    isRegex: bool
    isEqual: bool = True  # API v2 明确默认值；结果字段，不是模型输入。


class _SilenceStatus(_Model):
    state: Literal["expired", "active", "pending"]


class _Silence(_Model):
    id: Annotated[str, AfterValidator(_uuid)]
    matchers: list[_Matcher] = Field(min_length=1)
    startsAt: _Time
    endsAt: _Time
    updatedAt: _Time
    status: _SilenceStatus
    comment: str


class _RawGroup(_Model):
    labels: dict[str, str]
    receiver: _Receiver
    alerts: list[_Alert]


class _Group(_Model):
    group_index: int
    labels: dict[str, str]
    receiver: _Receiver
    alert_count: int
    alerts_truncated: bool


class _GroupAlerts(_Model):
    group_index: int
    alerts: list[_Alert]


class _Page(_Model):
    total: int
    offset: int
    limit: int
    has_more: bool


class _Alerts(_Page):
    data: list[_Alert]


class _Groups(_Page):
    data: list[_Group]
    alerts: list[_GroupAlerts]


class _Silences(_Page):
    data: list[_Silence]


class _Receivers(_Page):
    data: list[_Receiver]


class _Cluster(_Model):
    status: Literal["ready", "settling", "disabled"]


class _Version(_Model):
    version: str


class _RawStatus(_Model):
    cluster: _Cluster
    versionInfo: _Version
    uptime: _Time


class _Status(_Model):
    cluster_status: str
    version: str
    uptime: _Time


# 每项都有实际消费者：目录/治理与下面的固定 GET；没有可扩展注册机制。
_SPECS: dict[str, tuple[type[BaseModel], type[BaseModel], str, str]] = {
    "get_alerts": (
        _AlertArgs,
        _Alerts,
        "alerts",
        "读取告警快照。filters 是重复的标签 matcher，可按已知主机/告警收窄；空列表不限。"
        "active/silenced/inhibited/unprocessed 为包含开关，true 同时保留其他状态，不是只选该状态。"
        "receiver 是接收器正则，空串不限。silencedBy/inhibitedBy/mutedBy 分列；不证明已通知。",
    ),
    "get_alert_groups": (
        _GroupArgs,
        _Groups,
        "alerts/groups",
        "读取分组告警快照，filters 可收窄，receiver 是正则或空串不限。"
        "布尔值表示包含对应状态；muted=false 排除全部被抑制的分组。每组最多保留100条告警，"
        "data 保留分组摘要，alerts 单独投影；用 group_index 关联，它仅是本次响应内序号。"
        "alert_count 是省略前数量，alerts_truncated 表示组内省略。接收器名不证明已通知。",
    ),
    "get_silences": (
        _FilterArgs,
        _Silences,
        "silences",
        "读取静默快照，可用 filters 收窄；包含 active/pending/expired。"
        "静默存在不证明命中告警，按告警 silencedBy 的 ID 关联；起止时间和状态按采集时刻解释。",
    ),
    "get_silence": (
        _SilenceArgs,
        _Silence,
        "silence",
        "直接读取已知 UUID 的静默快照，无需先列表。保留匹配器、起止时间和状态；"
        "过期静默不表示当前抑制，存在也不证明命中告警。",
    ),
    "get_receivers": (
        _PageArgs,
        _Receivers,
        "receivers",
        "读取接收器名称目录，不读取通知目的地；名称不证明通知已发送。",
    ),
    "get_status": (
        _Args,
        _Status,
        "status",
        "读取 Alertmanager 自身集群状态、版本和 uptime（启动时间）。原配置和 peer 地址不保存，"
        "不向模型、Session 或渠道暴露；"
        "服务状态不证明主机健康或通知已发送。",
    ),
}

_LIST_READERS: dict[str, TypeAdapter[Any]] = {
    "get_alerts": TypeAdapter(list[_Alert]),
    "get_alert_groups": TypeAdapter(list[_RawGroup]),
    "get_silences": TypeAdapter(list[_Silence]),
    "get_receivers": TypeAdapter(list[_Receiver]),
}


def alertmanager_catalog(
    sources: tuple[AlertmanagerConfig, ...], projection_bytes: Mapping[Audience, int]
) -> tuple[tuple[ToolContract, ...], tuple[ToolPolicy, ...]]:
    contracts: list[ToolContract] = []
    policies: dict[str, ToolPolicy] = {}
    for source in sources:
        for name in source.tools:
            args, result, _, description = _SPECS[name]
            fields = tuple(result.model_fields)
            paged = issubclass(result, _Page)
            policy_id = f"alertmanager.{name}"
            policies[policy_id] = ToolPolicy(
                policy_id=policy_id,
                arguments=args,
                result=result,
                projections={a: Projection(fields, projection_bytes[a]) for a in AUDIENCES},
                required=tuple(_Page.model_fields) if paged else fields,
                fact_note=(
                    "Alertmanager API v2 采集快照，不能证明通知发送或主机健康。"
                    + (
                        "本地分页 total/has_more 仅描述此次过滤响应；投影截断时内容仍不完整，"
                        "has_more=false 也不能证明完整。"
                        if paged
                        else "历史快照不证明当前状态。"
                    )
                ),
            )
            contracts.append(
                ToolContract(
                    tool_id=source.tool_id(name),
                    target_id=source.server_id,
                    policy_id=policy_id,
                    input_schema=args.model_json_schema(),
                    description=description
                    + (
                        "limit 1–100、offset≥0；有界接收完整响应后本地分页，分页不是跨页一致快照。"
                        "查看 has_more、truncated 与组内标记；空页/部分结果不能证明源内无告警。"
                        if paged
                        else ""
                    ),
                )
            )
        for action_name in source.write_actions:
            args = _CreateSilenceArgs if action_name == "create_silence" else _SilenceArgs
            proposal_id = f"alertmanager.propose_{action_name}"
            write_id = f"alertmanager.{action_name}"
            policies[proposal_id] = proposal_policy(
                proposal_id, args, min(projection_bytes.values())
            )
            policies[write_id] = ToolPolicy(
                write_id,
                args,
                {
                    a: Projection(tuple(_Silence.model_fields), projection_bytes[a])
                    for a in AUDIENCES
                },
                required=tuple(_Silence.model_fields),
                effect="write",
                group_only=True,
                fact_note="批准后实际回读的静默快照；不证明后续通知发送。",
            )
            for tool, policy in (
                (f"propose_{action_name}", proposal_id),
                (action_name, write_id),
            ):
                contracts.append(
                    ToolContract(
                        tool_id=source.tool_id(tool),
                        target_id=source.server_id,
                        policy_id=policy,
                        input_schema=args.model_json_schema(),
                        description=(
                            "仅在本群提出创建静默，不执行。"
                            "matchers 每项显式 name/value/isRegex/isEqual，"
                            "正则整串匹配；starts_at/ends_at 为含时区绝对时间，允许多年但必须到期。"
                            "过去 starts_at 会由上游调整到批准执行时刻，不表示从过去已抑制告警。"
                            "准确范围/期限不明确时澄清，不扩大范围猜测。comment 写明用途。"
                            if action_name == "create_silence"
                            else "仅在本群提出按 UUID 提前结束静默，不执行也不移除记录。"
                            "可直接给已知 silence_id，无须固定先查列表；"
                            "显示当前对象与影响后等本次批准。"
                        ),
                    )
                )
    return tuple(contracts), tuple(policies.values())


class AlertmanagerAdapter:
    """每源一个异步客户端；状态只记录最后一次实际读取，不主动探测。"""

    def __init__(self, config: AlertmanagerConfig, *, clock: Callable[[], datetime]) -> None:
        self.config = config
        self._clock = clock
        self._client: httpx2.AsyncClient | None = None
        self.status = "configured"

    async def __aenter__(self) -> "AlertmanagerAdapter":
        auth = None
        if self.config.username_ref is not None and self.config.password_ref is not None:
            auth = httpx2.BasicAuth(
                resolve_secret_ref(self.config.username_ref).get_secret_value(),
                resolve_secret_ref(self.config.password_ref).get_secret_value(),
            )
        self._client = httpx2.AsyncClient(
            auth=auth,
            trust_env=False,
            follow_redirects=False,
            timeout=self.config.timeout_seconds,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client is not None:
            await self._client.aclose()

    @property
    def executes(self) -> dict[tuple[str, str], Execute]:
        return {
            (self.config.tool_id(name), self.config.server_id): Prechecked(
                self._prepare, self._read
            )
            for name in self.config.tools
        }

    def _prepare(self, request: ToolRequest) -> ToolRequest:
        # 参数已经由目录严格规范化；工具名、目标与固定路径仍由可信登记决定。
        name = request.tool_id.partition("/")[2]
        if request.target_id != self.config.server_id or name not in self.config.tools:
            raise ValueError("Alertmanager 执行绑定不符")
        return request

    def action_bindings(self, catalog: ToolCatalog) -> tuple[ActionBinding, ...]:
        bindings = []
        for name in self.config.write_actions:
            proposal = catalog.contract(
                self.config.tool_id(f"propose_{name}"), self.config.server_id
            )
            write = catalog.contract(self.config.tool_id(name), self.config.server_id)
            if proposal is None or write is None:
                raise ValueError("Alertmanager 动作未登记")
            bindings.append(
                ActionBinding(
                    proposal,
                    write,
                    self._plan,
                    Prechecked(self._prepare_write, self._write),
                    target_binding=arguments_digest(
                        {"config": self.config.model_dump(mode="json"), "semantics": "silence-v1"}
                    ),
                    precheck=self._prepare_write,
                )
            )
        return tuple(bindings)

    def _prepare_write(self, request: ToolRequest) -> ToolRequest:
        name = request.tool_id.partition("/")[2].removeprefix("propose_")
        if request.target_id != self.config.server_id or name not in self.config.write_actions:
            raise ToolRejectedError("动作不可用", code="not_allowed")
        if name == "create_silence":
            if datetime.fromisoformat(str(request.arguments["ends_at"])) <= self._clock():
                raise ToolRejectedError("静默结束时间已到", hint="silence_end")
        return request

    async def _impact(self, matchers: list[dict[str, Any]]) -> str:
        filters = [
            (
                "filter",
                json.dumps(m["name"], ensure_ascii=False)
                + ("=" if m["isEqual"] else "!")
                + ("~" if m["isRegex"] else "=" if not m["isEqual"] else "")
                + json.dumps(m["value"], ensure_ascii=False),
            )
            for m in matchers
        ]
        # 四种告警状态全部纳入当前影响；没有上游分页或长期命中预测。
        params = [
            *filters,
            *((flag, "true") for flag in ("active", "silenced", "inhibited", "unprocessed")),
        ]
        try:
            body = await self._request("GET", "alerts", params=tuple(params))
        except _ResponseLimitError:
            return (
                "当前命中数量未知：过滤响应超过接收上限，无法完整计数；"
                "无可核实样例。未来命中也受此匹配器影响。"
            )
        alerts = sorted(
            _LIST_READERS["get_alerts"].validate_json(body), key=lambda a: a.fingerprint
        )
        samples = [
            {
                "fingerprint": a.fingerprint,
                "labels": {
                    k: v[:128] + ("…" if len(v) > 128 else "")
                    for k, v in a.labels.items()
                    if k in {"alertname", "instance", "job"}
                },
            }
            for a in alerts[:5]
        ]
        return (
            f"当前命中 {len(alerts)} 条告警；最多5个标签/指纹样例（长标签省略）："
            + json.dumps(samples, ensure_ascii=False, sort_keys=True)
            + "。仅为此次过滤快照，未来命中也受此匹配器影响；静默不证明主机健康或通知发送。"
        )

    async def _silence(self, sid: str) -> _Silence:
        silence = _Silence.model_validate_json(await self._request("GET", f"silence/{sid}"))
        if silence.id != sid:
            raise ValueError("Alertmanager 静默 ID 不符")
        return silence

    async def _plan(self, request: ToolRequest) -> ActionPlan:
        self._prepare_write(request)
        args = request.arguments
        if request.tool_id.endswith("create_silence"):
            impact = await self._impact(cast(list[dict[str, Any]], args["matchers"]))
            return ActionPlan(
                parameters=args,
                revision=arguments_digest({"parameters": args, "impact": impact}),
                change="新建静默："
                + json.dumps(args, ensure_ascii=False, sort_keys=True)
                + "。过去开始时间由服务端归到执行时刻；结束时间保持批准值。",
                impact=impact,
                rollback="按执行结果的静默 UUID 另提取消静默并批准；不自动撤销。",
            )
        current = await self._silence(str(args["silence_id"]))
        if current.status.state == "expired":
            raise ValueError("静默已经到期，未提出取消")
        snapshot = current.model_dump(mode="json")
        impact = await self._impact(snapshot["matchers"])
        return ActionPlan(
            parameters=args,
            revision=arguments_digest({"silence": snapshot, "impact": impact}),
            change="提前结束并保留静默记录："
            + json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            impact=impact,
            rollback="取消不可恢复原记录；若需重新静默，按原匹配器/明确到期时间另提创建并批准。",
        )

    async def _write(self, request: ToolRequest) -> ToolObservation:
        self._prepare_write(request)
        args = request.arguments
        async with asyncio.timeout(self.config.timeout_seconds):
            if request.tool_id.endswith("/create_silence"):
                requested_start = datetime.fromisoformat(str(args["starts_at"]))
                start_is_future = requested_start > self._clock()
                response = await self._request(
                    "POST",
                    "silences",
                    payload={
                        "matchers": args["matchers"],
                        "startsAt": args["starts_at"],
                        "endsAt": args["ends_at"],
                        "comment": args["comment"],
                        "createdBy": "xiaowei",
                    },
                )
                body = json.loads(response)
                if (
                    not isinstance(body, dict)
                    or set(body) != {"silenceID"}
                    or not isinstance(body["silenceID"], str)
                ):
                    raise ValueError("Alertmanager 创建结果不合约")
                sid = _uuid(body["silenceID"])
                actual = await self._silence(sid)
                actual_start = datetime.fromisoformat(actual.startsAt)
                if (
                    sorted(
                        (m.model_dump() for m in actual.matchers),
                        key=lambda m: json.dumps(m, sort_keys=True),
                    )
                    != sorted(
                        cast(list[dict[str, Any]], args["matchers"]),
                        key=lambda m: json.dumps(m, sort_keys=True),
                    )
                    or actual.comment != args["comment"]
                    or datetime.fromisoformat(actual.endsAt)
                    != datetime.fromisoformat(str(args["ends_at"]))
                    or actual_start < requested_start
                    or (
                        actual_start != requested_start
                        and (
                            start_is_future
                            or actual.status.state != "active"
                            or actual_start > datetime.fromisoformat(actual.updatedAt)
                        )
                    )
                    or actual_start >= datetime.fromisoformat(actual.endsAt)
                    or actual.status.state == "expired"
                ):
                    raise ValueError("Alertmanager 创建回读不符")
            else:
                sid = str(args["silence_id"])
                await self._request("DELETE", f"silence/{sid}", empty=True)
                actual = await self._silence(sid)
                if actual.status.state != "expired":
                    raise ValueError("Alertmanager 取消回读不符")
        return ToolObservation(
            payload=actual.model_dump(mode="json"), captured_at=self._clock(), truncated=False
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: tuple[tuple[str, str], ...] = (),
        payload: dict[str, Any] | None = None,
        empty: bool = False,
    ) -> bytes:
        client = self._client
        if client is None:
            raise ValueError("Alertmanager 客户端未装配")
        async with asyncio.timeout(self.config.timeout_seconds):
            async with client.stream(
                method,
                f"{self.config.url}/api/v2/{path}",
                params=params,
                json=payload,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            ) as response:
                if response.status_code in {401, 403}:
                    raise MonitoringReadError("auth")
                if 500 <= response.status_code <= 599:
                    raise MonitoringReadError("upstream_5xx")
                if (
                    response.status_code != 200
                    or response.headers.get("content-encoding", "identity").lower() != "identity"
                    or (
                        not empty
                        and response.headers.get("content-type", "")
                        .partition(";")[0]
                        .strip()
                        .lower()
                        != "application/json"
                    )
                ):
                    raise ValueError("Alertmanager 响应不合约")
                data = bytearray()
                async for chunk in response.aiter_raw():
                    if len(data) + len(chunk) > self.config.max_response_bytes:
                        raise _ResponseLimitError("Alertmanager 响应超限")
                    data.extend(chunk)
                if empty and data:
                    raise ValueError("Alertmanager 取消结果不合约")
                return bytes(data)

    async def _read(self, request: ToolRequest) -> ToolObservation:
        name = request.tool_id.partition("/")[2]
        args = request.arguments
        path = _SPECS[name][2]
        if name == "get_silence":
            path += "/" + str(args["silence_id"])
        params: list[tuple[str, str]] = []
        for key, value in args.items():
            if key == "filters":
                params.extend(("filter", matcher) for matcher in cast(list[str], value))
            elif type(value) is bool:
                params.append((key, "true" if value else "false"))
            elif key == "receiver" and value:
                params.append((key, str(value)))
        try:
            payload, truncated = self._project(
                name, await self._request("GET", path, params=tuple(params)), args
            )
        except (TimeoutError, httpx2.TimeoutException):
            self.status = "unavailable"
            raise MonitoringReadError("timeout") from None
        except httpx2.NetworkError:
            self.status = "unavailable"
            raise MonitoringReadError("unavailable") from None
        except Exception:
            self.status = "unavailable"
            raise
        self.status = "available"
        return ToolObservation(payload=payload, captured_at=self._clock(), truncated=truncated)

    @staticmethod
    def _project(
        name: str, body: bytes, args: Mapping[str, object]
    ) -> tuple[dict[str, object], bool]:
        if name == "get_status":
            status = _RawStatus.model_validate_json(body)
            return {
                "cluster_status": status.cluster.status,
                "version": status.versionInfo.version,
                "uptime": status.uptime,
            }, False
        if name == "get_silence":
            silence = _Silence.model_validate_json(body)
            if silence.id != args["silence_id"]:
                raise ValueError("Alertmanager 静默 ID 不符")
            return silence.model_dump(mode="json"), False
        values: list[_Alert | _RawGroup | _Silence | _Receiver] = _LIST_READERS[name].validate_json(
            body
        )
        offset, limit = cast(int, args["offset"]), cast(int, args["limit"])
        has_more = offset + limit < len(values)
        selected = values[offset : offset + limit]
        data = []
        truncated = has_more or offset > 0
        groups_alerts = []
        for index, value in enumerate(selected, start=offset):
            row = value.model_dump(mode="json")
            if isinstance(value, _RawGroup):
                count = len(value.alerts)
                groups_alerts.append({"group_index": index, "alerts": row.pop("alerts")[:100]})
                row.update(
                    group_index=index,
                    alert_count=count,
                    alerts_truncated=count > 100,
                )
                truncated |= count > 100
            data.append(row)
        payload = {
            "data": data,
            "total": len(values),
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
        }
        if name == "get_alert_groups":
            payload["alerts"] = groups_alerts
        return payload, truncated
