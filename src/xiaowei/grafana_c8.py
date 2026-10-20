"""C8 选中 Grafana 写工具的薄契约；审批和执行事实仍由 ActionService 管理。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qsl, urlsplit

from mcp.types import CallToolResult, ResourceLink, TextContent
from pydantic import BaseModel, ConfigDict, Field

from xiaowei.config import MCPServerConfig
from xiaowei.evidence import arguments_digest
from xiaowei.governance import (
    Prechecked,
    Projection,
    ProposalRejectedError,
    ToolCatalog,
    ToolPolicy,
    ToolRejectedError,
    schema_shape,
)
from xiaowei.models import AUDIENCES, Audience, ToolContract, ToolObservation, ToolRequest

if TYPE_CHECKING:
    from xiaowei.actions import ActionBinding
    from xiaowei.mcp import MCPIntegration

C8_SCHEMAS = {
    "create_annotation": "20f67554334bf68669dc5584592c3c455184a8d558ab93e24d06d8bb43c33493",
    "create_datasource": "0598f5d75f84c4ce5b89ec7d36683f1df33c27e82f5a93966e76664dd21e2f26",
    "update_datasource": "b9a4ec959223882f7b29236fdcc9a1a575031e3665a24ad388e7826390d65e1c",
    "get_datasource": "25e98dfb8425e0f07867ad457c44657732e00daa7b3b09c8d9af98902d1d1927",
}
C8_POLICIES = frozenset(f"grafana.{name}" for name in C8_SCHEMAS)
_UID = re.compile(r"[a-zA-Z0-9_-]{1,40}")
_MAX_MILLISECONDS = 253402300799000
_CREATE_SETTINGS = frozenset(
    {"access", "basicAuth", "database", "fields", "isDefault", "url", "withCredentials"}
)
_UPDATE_SETTINGS = frozenset(
    {"access", "basicAuth", "database", "fields", "isDefault", "jsonData", "name", "url"}
)


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AnnotationArgs(_Args):
    text: str
    time: int
    time_end: int
    tags: list[str]
    dashboard_uid: str
    panel_id: int = Field(ge=0)


class DatasourceCreateArgs(_Args):
    name: str
    plugin_type: str
    settings_json: str


class DatasourceUpdateArgs(_Args):
    uid: str
    settings_json: str


class DatasourceUpdateWriteArgs(DatasourceUpdateArgs):
    expected_digest: str


class DatasourceReadArgs(_Args):
    uid: str


class _WriteResult(_Args):
    result_json: str


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(_value: str) -> None:
    raise ValueError("non-finite JSON value")


def _credential_field(name: str) -> bool:
    key = re.sub(r"[^a-z0-9]", "", name.casefold())
    return any(part in key for part in ("password", "token", "apikey", "secret"))


def _has_value(value: object) -> bool:
    return value not in (None, "", {}, [])


def _url_has_credentials(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.username is not None
        or parsed.password is not None
        or any(
            _credential_field(key) and _has_value(item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        )
    )


def _safe_readback(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: "[已隐藏]" if _credential_field(key) and _has_value(item) else _safe_readback(item)
            for key, item in value.items()
            if key.casefold() != "securejsondata"
        }
    if isinstance(value, list):
        return [_safe_readback(item) for item in value]
    if isinstance(value, str) and "://" in value:
        try:
            if _url_has_credentials(value):
                return "[已隐藏含凭据URL]"
        except ValueError:
            return "[已隐藏无效URL]"
    return value


def _request_settings(text: object, *, update: bool) -> dict[str, Any]:
    if not isinstance(text, str) or len(text.encode()) > 12_000:
        raise ToolRejectedError("数据源设置须为不超过12000字节的JSON对象")
    try:
        settings = json.loads(text, object_pairs_hook=_pairs, parse_constant=_nonfinite)
        if not isinstance(settings, dict) or (update and not settings):
            raise ValueError
        allowed = _UPDATE_SETTINGS if update else _CREATE_SETTINGS
        if not settings.keys() <= allowed:
            raise ValueError
        if any(
            key in settings and not isinstance(settings[key], dict)
            for key in ("fields", "jsonData")
        ):
            raise ValueError
        _json(settings)
    except (ValueError, TypeError, RecursionError):
        raise ToolRejectedError("数据源设置须为锁版MCP参数对象；修改时至少一项") from None

    if "fields" in settings and "jsonData" in settings:
        raise ToolRejectedError("fields 与 jsonData 不能同时提交；请只选择一种修改方式")

    def check(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key.casefold() == "securejsondata":
                    raise ToolRejectedError("加密凭据请到Grafana手动配置，不得提交secureJsonData")
                if _credential_field(key) and _has_value(item):
                    raise ToolRejectedError("凭据字段不得通过群提交；数据源待配置凭据")
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
        elif isinstance(value, str) and "://" in value:
            try:
                if _url_has_credentials(value):
                    raise ToolRejectedError("URL不得包含账号、密码或Token；请到Grafana手动配置凭据")
            except ValueError:
                raise ToolRejectedError("URL格式不合法") from None

    check(settings)
    return settings


def _guidance_fields(result: CallToolResult, plugin_type: str) -> dict[str, str] | None:
    if (
        result.is_error
        or result.result_type != "complete"
        or result.structured_content is not None
        or len(result.content) != 1
        or not isinstance(result.content[0], TextContent)
    ):
        raise ValueError("数据源字段说明不符合锁版契约")
    guidance = json.loads(result.content[0].text, object_pairs_hook=_pairs)
    if not isinstance(guidance, dict) or guidance.get("type") != plugin_type:
        raise ValueError("数据源字段说明的插件类型不符")
    if "fields" not in guidance:
        if not isinstance(guidance.get("message"), str):
            raise ValueError("无schema插件的字段说明不符")
        return None
    fields = guidance["fields"]
    if not isinstance(fields, list) or any(
        not isinstance(field, dict)
        or not isinstance(field.get("key"), str)
        or not field["key"]
        or not isinstance(field.get("type"), str)
        for field in fields
    ):
        raise ValueError("数据源字段说明的字段列表不符")
    guidance_types = {field["key"]: field["type"] for field in fields}
    if len(guidance_types) != len(fields):
        raise ValueError("数据源字段说明有重复键")
    return guidance_types


def schema_matches_c8(name: str, schema: dict[str, object]) -> bool:
    shape = schema_shape(schema, ignore_extra_flags=True)
    return C8_SCHEMAS.get(name) == hashlib.sha256(_json(shape).encode()).hexdigest()


def prepare_annotation(request: ToolRequest) -> ToolRequest:
    args = request.arguments
    text = args["text"]
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
        raise ToolRejectedError("Annotation文本须为1至2000字符")
    start, end = args["time"], args["time_end"]
    if type(start) is not int or type(end) is not int or not 0 <= start <= end <= _MAX_MILLISECONDS:
        raise ToolRejectedError("Annotation时间须为有序Unix毫秒，结束不能早于开始")
    tags = args["tags"]
    if (
        not isinstance(tags, list)
        or len(tags) > 32
        or any(not isinstance(tag, str) or not 1 <= len(tag) <= 256 for tag in tags)
    ):
        raise ToolRejectedError("Annotation标签最多32项，每项1至256字符")
    uid, panel = args["dashboard_uid"], args["panel_id"]
    if not isinstance(uid, str) or (uid and _UID.fullmatch(uid) is None):
        raise ToolRejectedError("Dashboard UID不符合Grafana格式")
    if type(panel) is not int or not 0 <= panel <= 2147483647 or (panel and not uid):
        raise ToolRejectedError("panel_id须为非负整数，指定面板时也须指定Dashboard UID")
    return request


def prepare_datasource(request: ToolRequest) -> ToolRequest:
    args = request.arguments
    name = args.get("name")
    plugin_type = args.get("plugin_type")
    uid = args.get("uid")
    if name is not None and (not isinstance(name, str) or not 1 <= len(name.strip()) <= 200):
        raise ToolRejectedError("数据源名称须为1至200字符")
    if plugin_type is not None and (
        not isinstance(plugin_type, str) or not 1 <= len(plugin_type.strip()) <= 200
    ):
        raise ToolRejectedError("插件类型须为非空字符串")
    if uid is not None and (not isinstance(uid, str) or _UID.fullmatch(uid) is None):
        raise ToolRejectedError("数据源UID不符合Grafana格式")
    if "settings_json" in args:
        _request_settings(args["settings_json"], update="uid" in args)
    return request


def wire_arguments_c8(name: str, args: dict[str, object]) -> dict[str, object]:
    if name == "create_annotation":
        return {
            "text": args["text"],
            "time": args["time"],
            "timeEnd": args["time_end"],
            "tags": args["tags"],
            **({"dashboardUid": args["dashboard_uid"]} if args["dashboard_uid"] else {}),
            **({"panelId": args["panel_id"]} if args["panel_id"] else {}),
        }
    if name == "get_datasource":
        return {"uid": args["uid"]}
    if name in {"create_datasource", "update_datasource"}:
        settings = _request_settings(args["settings_json"], update=name == "update_datasource")
        return {
            **(
                {"uid": args["uid"]}
                if name == "update_datasource"
                else {"name": args["name"], "type": args["plugin_type"]}
            ),
            **settings,
            "schemaReviewed": True,
        }
    raise ValueError("C8工具未开放")


def wire_payload_c8(result: CallToolResult, name: str) -> dict[str, object]:
    if name in {"create_datasource", "update_datasource", "get_datasource"}:
        return _datasource_payload(result, name)
    if (
        name != "create_annotation"
        or result.is_error
        or result.result_type != "complete"
        or result.structured_content is not None
        or len(result.content) != 1
        or not isinstance(result.content[0], TextContent)
    ):
        raise ValueError("Annotation MCP结果不符合锁版契约")
    value = json.loads(result.content[0].text)
    if (
        not isinstance(value, dict)
        or set(value) != {"Payload"}
        or not isinstance(value["Payload"], dict)
        or set(value["Payload"]) != {"id", "message"}
        or type(value["Payload"]["id"]) is not int
        or value["Payload"]["id"] <= 0
        or value["Payload"]["message"] != "Annotation added"
    ):
        raise ValueError("Annotation创建结果不符")
    return {"result_json": _json({"id": value["Payload"]["id"]})}


def _datasource_payload(result: CallToolResult, name: str) -> dict[str, object]:
    if (
        result.is_error
        or result.result_type != "complete"
        or result.structured_content is not None
        or len(result.content) not in (1, 2)
        or not isinstance(result.content[0], TextContent)
        or (
            len(result.content) == 2
            and (name != "create_datasource" or not isinstance(result.content[1], ResourceLink))
        )
    ):
        raise ValueError("数据源MCP结果不符合锁版契约")
    value = json.loads(result.content[0].text)
    if not isinstance(value, dict):
        raise ValueError("数据源结果不是对象")
    if name == "create_datasource":
        if (
            value.get("message") != "Datasource added"
            or type(value.get("id")) is not int
            or value["id"] <= 0
            or not isinstance(value.get("uid"), str)
            or not isinstance(value.get("name"), str)
        ):
            raise ValueError("数据源创建结果不符")
        return {"result_json": _json({k: value[k] for k in ("id", "uid", "name")})}
    if name == "update_datasource":
        if value.get("message") != "Datasource updated":
            raise ValueError("数据源修改结果不符")
        return {"result_json": _json({"message": "Datasource updated"})}
    if (
        not isinstance(value.get("uid"), str)
        or not isinstance(value.get("name"), str)
        or not isinstance(value.get("type"), str)
        or type(value.get("version")) is not int
        or value["version"] < 1
        or not isinstance(value.get("url"), str)
        or not isinstance(value.get("jsonData", {}), dict)
    ):
        raise ValueError("数据源回读结果不符")
    # 远端返回的凭据及作者信息不进入 Action、Evidence 或模型；原对象摘要只供冲突复核。
    safe = {
        key: _safe_readback(value[key])
        for key in ("id", "uid", "name", "type", "url", "access", "version", "orgId")
        if key in value
    }
    safe["jsonData"] = _safe_readback(value.get("jsonData", {}))
    safe["digest"] = hashlib.sha256(_json(value).encode()).hexdigest()
    return {"result_json": _json(safe)}


def annotation_catalog(
    configs: Iterable[MCPServerConfig], limits: dict[Audience, int]
) -> tuple[tuple[ToolContract, ...], tuple[ToolPolicy, ...]]:
    from xiaowei.actions import proposal_policy

    sources = [
        s
        for s in configs
        if s.allowed_tools.get("create_annotation") == "grafana.create_annotation"
    ]
    if not sources:
        return (), ()
    write_policy = ToolPolicy(
        "grafana.create_annotation",
        AnnotationArgs,
        {a: Projection(("result_json",), limits[a]) for a in AUDIENCES},
        result=_WriteResult,
        required=("result_json",),
        effect="write",
        group_only=True,
        fact_note="Annotation写结果以批准后的回读为准，不证明告警或主机健康。",
    )
    proposal = proposal_policy(
        "grafana.propose_create_annotation", AnnotationArgs, min(limits.values())
    )
    policies = (write_policy, proposal)
    contracts = tuple(
        ToolContract(
            tool_id=s.tool_id(name),
            target_id=s.server_id,
            policy_id=f"grafana.{name}",
            input_schema=policy.arguments.model_json_schema(),
            description=(
                "仅供确定性群批准执行新增Annotation；不进入Runner。"
                if name == "create_annotation"
                else "提出新增Grafana Annotation，不执行。给出文本、Unix毫秒起止、标签；"
                "可指定Dashboard UID和面板ID。代码显示完整内容并等待本次群批准。"
            ),
        )
        for s in sources
        for name, policy in (
            ("create_annotation", write_policy),
            ("propose_create_annotation", proposal),
        )
    )
    return contracts, policies


def datasource_catalog(
    configs: Iterable[MCPServerConfig], limits: dict[Audience, int]
) -> tuple[tuple[ToolContract, ...], tuple[ToolPolicy, ...]]:
    from xiaowei.actions import proposal_policy

    selected = [
        s
        for s in configs
        if any(
            s.allowed_tools.get(name) == f"grafana.{name}"
            for name in ("create_datasource", "update_datasource")
        )
    ]
    if not selected:
        return (), ()
    policies = {
        "get_datasource": ToolPolicy(
            "grafana.get_datasource",
            DatasourceReadArgs,
            {a: Projection(("result_json",), limits[a]) for a in AUDIENCES},
            result=_WriteResult,
            required=("result_json",),
            fact_note="数据源配置快照，健康检查未验证；秘密字段不会进入结果。",
            fact_note_summary="数据源配置快照，健康检查未验证。",
        ),
        "create_datasource": ToolPolicy(
            "grafana.create_datasource",
            DatasourceCreateArgs,
            {a: Projection(("result_json",), limits[a]) for a in AUDIENCES},
            result=_WriteResult,
            required=("result_json",),
            effect="write",
            group_only=True,
        ),
        "update_datasource": ToolPolicy(
            "grafana.update_datasource",
            DatasourceUpdateWriteArgs,
            {a: Projection(("result_json",), limits[a]) for a in AUDIENCES},
            result=_WriteResult,
            required=("result_json",),
            effect="write",
            group_only=True,
        ),
        "propose_create_datasource": proposal_policy(
            "grafana.propose_create_datasource", DatasourceCreateArgs, min(limits.values())
        ),
        "propose_update_datasource": proposal_policy(
            "grafana.propose_update_datasource", DatasourceUpdateArgs, min(limits.values())
        ),
    }
    descriptions = {
        "get_datasource": "读取当前数据源配置与版本，凭据值会隐藏；仅在本轮获准工具范围内可见。",
        "create_datasource": "仅供群批准后创建数据源；不进入Runner。",
        "update_datasource": "仅供群批准后修改数据源；不进入Runner。",
        "propose_create_datasource": (
            "提出创建Grafana数据源，不执行。name为显示名，plugin_type为插件类型（不设类型白名单）；"
            "settings_json是MCP参数JSON对象；有字段说明的插件把合法动态字段放fields内，例如"
            '{"fields":{"url":"http://prometheus:9090"}}；无字段说明的插件使用顶层url等参数。'
            "不确定字段名时按拒绝反馈中的allowed_fields修正。"
            "密码/Token须到Grafana手动配置，不提交secureJsonData或含账号密码/Token的URL。"
        ),
        "propose_update_datasource": (
            "提出修改Grafana数据源，不执行。uid指定现有对象；settings_json仅写要改的字段，"
            "有字段说明的插件把合法动态字段放fields内；无字段说明时使用顶层url等参数。"
            "不确定字段名时按allowed_fields修正。fields与jsonData不能同时提交。"
            "jsonData局部变更会合并当前非秘密字段并展示实际差异；"
            "旧值含隐藏字段时拒绝，需管理员在Grafana手动处理。"
            "密码/Token到Grafana手动配置，不提交secureJsonData或含账号密码/Token的URL。"
        ),
    }
    contracts = tuple(
        ToolContract(
            tool_id=s.tool_id(name),
            target_id=s.server_id,
            policy_id=policy.policy_id,
            input_schema=policy.arguments.model_json_schema(),
            description=descriptions[name],
        )
        for s in selected
        for name in (
            "get_datasource",
            "create_datasource",
            "update_datasource",
            "propose_create_datasource",
            "propose_update_datasource",
        )
        if name in s.allowed_tools
        or (name.startswith("propose_") and name.removeprefix("propose_") in s.allowed_tools)
        for policy in (policies[name],)
    )
    used = {contract.policy_id for contract in contracts}
    return contracts, tuple(policy for policy in policies.values() if policy.policy_id in used)


def _contains_hidden(value: object) -> bool:
    if isinstance(value, str):
        return value in {"[已隐藏]", "[已隐藏含凭据URL]", "[已隐藏无效URL]"}
    if isinstance(value, dict):
        return any(_contains_hidden(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_hidden(item) for item in value)
    return False


def _merge_json_data(current: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    if _contains_hidden(current):
        raise ToolRejectedError("现有jsonData含隐藏值，不能安全局部修改；请由管理员在Grafana处理")
    merged = dict(current)
    for key, value in changes.items():
        old = current.get(key)
        merged[key] = (
            _merge_json_data(old, value)
            if isinstance(old, dict) and isinstance(value, dict)
            else value
        )
    return merged


def datasource_actions(
    mcp: MCPIntegration,
    configs: Iterable[MCPServerConfig],
    catalog: ToolCatalog,
    *,
    clock: Callable[[], datetime],
) -> tuple[ActionBinding, ...]:
    from xiaowei.actions import ActionBinding, ActionPlan, ActionWriteRejectedError

    bindings: list[ActionBinding] = []
    for source in configs:
        actions = [
            name
            for name in ("create_datasource", "update_datasource")
            if source.allowed_tools.get(name) == f"grafana.{name}"
        ]
        if not actions:
            continue
        sid = source.server_id

        async def read(uid: str, s: MCPServerConfig = source) -> dict[str, Any]:
            req = ToolRequest(
                tool_id=s.tool_id("get_datasource"),
                target_id=s.server_id,
                call_id="datasource-readback",
                tool_name=s.sdk_tool_name("get_datasource"),
                arguments={"uid": uid},
            )
            execute = mcp.execute_for(req.tool_id, req.target_id)
            value = await (
                execute.run(execute.check(req)) if isinstance(execute, Prechecked) else execute(req)
            )
            decoded = json.loads(str(value.payload["result_json"]))
            if not isinstance(decoded, dict) or decoded.get("uid") != uid:
                raise ValueError("数据源回读UID不符")
            return decoded

        async def plan(
            req: ToolRequest,
            load: Callable[..., Any] = read,
            s: MCPServerConfig = source,
        ) -> ActionPlan:
            args = req.arguments
            update = "uid" in args
            settings = _request_settings(args["settings_json"], update=update)
            before = await load(args["uid"]) if update else None
            plugin_type = cast(str, before["type"] if before is not None else args["plugin_type"])
            guidance = await mcp.datasource_guidance(
                s.server_id,
                "update_datasource" if update else "create_datasource",
                cast(str, args["uid"]) if update else plugin_type,
            )
            allowed = _guidance_fields(guidance, plugin_type)
            if allowed is None and "fields" in settings:
                raise ProposalRejectedError("无schema插件不接受fields", hint="datasource_no_schema")
            fields = settings.get("fields", {})
            if allowed is not None:
                allowed_keys = tuple(sorted(allowed))
                if set(fields) - allowed.keys():
                    raise ProposalRejectedError(
                        "fields含未知插件字段",
                        hint="datasource_fields",
                        allowed_fields=allowed_keys,
                    )
                for key, value in fields.items():
                    expected = allowed[key]
                    valid = (
                        (expected == "string" and isinstance(value, str))
                        or (expected == "boolean" and type(value) is bool)
                        or (expected == "number" and type(value) in (int, float))
                        or (expected == "integer" and type(value) is int)
                        or expected not in {"string", "boolean", "number", "integer"}
                    )
                    if not valid:
                        raise ProposalRejectedError(
                            "fields字段值类型与说明不符",
                            hint="datasource_field_type",
                            allowed_fields=allowed_keys,
                        )
            if before is not None and "jsonData" in settings:
                settings["jsonData"] = _merge_json_data(before["jsonData"], settings["jsonData"])
                settings = _request_settings(_json(settings), update=True)
            params = {
                **(
                    {"uid": args["uid"]}
                    if update
                    else {"name": args["name"], "plugin_type": args["plugin_type"]}
                ),
                "settings_json": _json(settings),
            }
            if before is not None:
                params["expected_digest"] = before["digest"]
            return ActionPlan(
                parameters=params,
                revision=before["digest"] if before is not None else arguments_digest(params),
                change=_json({"before": before, "requested_after": params}),
                impact=(
                    "修改此数据源的所列字段；Grafana插件可能进一步规范化配置。需要密码时待配置凭据，"
                    "由管理员在Grafana设置页手动填写；健康检查通过前不称连接可用。"
                    if update
                    else "创建此数据源；需要密码时待配置凭据，由管理员在Grafana设置页手动填写；"
                    "健康检查通过前不称连接可用。"
                ),
                rollback=(
                    "需再次群审批，先回读当前完整配置，再按before中的非秘密字段提出恢复；"
                    "秘密配置由管理员在Grafana手动处理。"
                    if update
                    else "如需删除，由Grafana管理员按写后回执UID人工删除并记录；小维不开放删除。"
                ),
            )

        def available(name: str, s: MCPServerConfig = source) -> bool:
            try:
                mcp.execute_for(s.tool_id(name), s.server_id)
                mcp.execute_for(s.tool_id("get_datasource"), s.server_id)
            except ToolRejectedError:
                return False
            return True

        def checked(
            req: ToolRequest,
            name: str,
            ready: Callable[[str], bool] = available,
        ) -> ToolRequest:
            prepare_datasource(req)
            if not ready(name):
                raise ToolRejectedError("数据源能力未核约或源不可用", code="unsupported")
            return req

        async def write(req: ToolRequest, load: Callable[..., Any] = read) -> ToolObservation:
            args = req.arguments
            update = "uid" in args
            if update:
                current = await load(args["uid"])
                if current["digest"] != args["expected_digest"]:
                    raise ActionWriteRejectedError("数据源配置已变化，未执行，请重新提出")
            execute = mcp.execute_for(req.tool_id, req.target_id)
            result = await (
                execute.run(execute.check(req)) if isinstance(execute, Prechecked) else execute(req)
            )
            saved = json.loads(str(result.payload["result_json"]))
            uid = args["uid"] if update else saved["uid"]
            if not update and saved["name"] != args["name"]:
                raise ValueError("数据源创建响应名称不符")
            actual = await load(uid)
            if (update and actual["digest"] == args["expected_digest"]) or (
                not update
                and (actual["name"] != args["name"] or actual["type"] != args["plugin_type"])
            ):
                raise ValueError("数据源写后回读身份或配置未变化")
            settings = _request_settings(args["settings_json"], update=update)
            for key, value in settings.items():
                if key == "fields":
                    for field_name, field_value in value.items():
                        if not _field_matches(actual, field_name, field_value):
                            raise ValueError("数据源写后字段不符")
                elif key == "jsonData":
                    if _json(actual["jsonData"]) != _json(value):
                        raise ValueError("数据源写后jsonData不符")
                elif not _field_matches(actual, key, value):
                    raise ValueError("数据源写后字段不符")
            return ToolObservation(
                payload={
                    "result_json": _json(
                        {
                            "uid": uid,
                            "name": actual["name"],
                            "type": actual["type"],
                            "version": actual["version"],
                            "url": actual["url"],
                            "setup_notice": (
                                "待配置凭据：如需密码或Token，请由管理员前往Grafana "
                                f"/connections/datasources/edit/{uid} 手动填写；"
                                "当前健康检查未验证，不代表数据源可用。"
                            ),
                        }
                    )
                },
                captured_at=clock(),
                truncated=False,
            )

        for name in actions:
            proposal = catalog.contract(source.tool_id(f"propose_{name}"), sid)
            write_contract = catalog.contract(source.tool_id(name), sid)
            if proposal is None or write_contract is None:
                raise ValueError("数据源动作未登记")

            def precheck_action(
                req: ToolRequest,
                selected: str = name,
                check: Callable[[ToolRequest, str], ToolRequest] = checked,
            ) -> ToolRequest:
                return check(req, selected)

            def available_action(
                selected: str = name,
                ready: Callable[[str], bool] = available,
            ) -> bool:
                return ready(selected)

            bindings.append(
                ActionBinding(
                    proposal,
                    write_contract,
                    plan,
                    Prechecked(prepare_datasource, write),
                    target_binding=arguments_digest(
                        {
                            "config": source.model_dump(mode="json"),
                            "semantics": "datasource-write-v1",
                        }
                    ),
                    precheck=precheck_action,
                    available=available_action,
                )
            )
    return tuple(bindings)


def _field_matches(actual: dict[str, Any], name: str, value: object) -> bool:
    for source in (actual, actual.get("jsonData", {})):
        if name in source and _json(source[name]) == _json(value):
            return True
    return False


def annotation_actions(
    mcp: MCPIntegration,
    configs: Iterable[MCPServerConfig],
    catalog: ToolCatalog,
    *,
    clock: Callable[[], datetime],
) -> tuple[ActionBinding, ...]:
    from xiaowei.actions import ActionBinding, ActionPlan

    bindings: list[ActionBinding] = []
    for source in configs:
        if source.allowed_tools.get("create_annotation") != "grafana.create_annotation":
            continue
        sid = source.server_id

        def available(s: MCPServerConfig = source) -> bool:
            try:
                mcp.execute_for(s.tool_id("create_annotation"), s.server_id)
                mcp.execute_for(s.tool_id("get_annotations"), s.server_id)
            except ToolRejectedError:
                return False
            return True

        def checked(req: ToolRequest, ready: Callable[[], bool] = available) -> ToolRequest:
            prepare_annotation(req)
            if not ready():
                raise ToolRejectedError("Annotation能力未核约或源不可用", code="unsupported")
            return req

        async def plan(req: ToolRequest) -> ActionPlan:
            args = req.arguments
            return ActionPlan(
                parameters=dict(args),
                revision=arguments_digest(args),
                change=_json({"before": None, "after": args}),
                impact=(
                    "在Grafana组织中新增Annotation；"
                    f"Dashboard={args['dashboard_uid'] or '组织范围'}，"
                    f"panel={args['panel_id']}，起止={args['time']}..{args['time_end']}。"
                ),
                rollback=(
                    "如需删除，Grafana管理员按执行回执中的Annotation ID人工删除并记录；"
                    "小维不开放删除。"
                ),
            )

        async def write(req: ToolRequest, s: MCPServerConfig = source) -> ToolObservation:
            execute = mcp.execute_for(req.tool_id, req.target_id)
            result = await (
                execute.run(execute.check(req)) if isinstance(execute, Prechecked) else execute(req)
            )
            created = json.loads(str(result.payload["result_json"]))
            annotation_id = created["id"]
            args = req.arguments
            read_req = ToolRequest(
                tool_id=s.tool_id("get_annotations"),
                target_id=s.server_id,
                call_id="annotation-readback",
                tool_name=s.sdk_tool_name("get_annotations"),
                arguments={
                    "from": args["time"],
                    "to": args["time_end"],
                    "limit": 100,
                    "dashboardUid": args["dashboard_uid"],
                    "tags": args["tags"],
                    "matchAny": False,
                },
            )
            reader = mcp.execute_for(read_req.tool_id, read_req.target_id)
            observed = await (
                reader.run(reader.check(read_req))
                if isinstance(reader, Prechecked)
                else reader(read_req)
            )
            rows = observed.payload["annotations"]
            if not isinstance(rows, list):
                raise ValueError("Annotation回读不符")
            matching = [row for row in rows if row["id"] == annotation_id]
            if len(matching) != 1:
                raise ValueError("Annotation回读缺少创建ID")
            actual = matching[0]
            expected = {
                "text": args["text"],
                "time": args["time"],
                "timeEnd": args["time_end"],
                "tags": args["tags"],
            }
            if any(actual.get(key) != value for key, value in expected.items()):
                raise ValueError("Annotation回读内容不符")
            if args["dashboard_uid"] and actual.get("dashboardUID") != args["dashboard_uid"]:
                raise ValueError("Annotation回读Dashboard不符")
            if args["panel_id"] and actual.get("panelId") != args["panel_id"]:
                raise ValueError("Annotation回读面板不符")
            return ToolObservation(
                payload={"result_json": _json({"id": annotation_id, **expected})},
                captured_at=clock(),
                truncated=False,
            )

        proposal = catalog.contract(source.tool_id("propose_create_annotation"), sid)
        write_contract = catalog.contract(source.tool_id("create_annotation"), sid)
        if proposal is None or write_contract is None:
            raise ValueError("Annotation动作未登记")
        bindings.append(
            ActionBinding(
                proposal,
                write_contract,
                plan,
                Prechecked(prepare_annotation, write),
                target_binding=arguments_digest(
                    {"config": source.model_dump(mode="json"), "semantics": "annotation-create-v1"}
                ),
                precheck=checked,
                available=available,
            )
        )
    return tuple(bindings)
