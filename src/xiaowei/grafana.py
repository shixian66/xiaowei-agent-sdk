"""D7 经典 Dashboard：选中 MCP 契约转换、可信差异与已有群 Action 绑定。

完整 Dashboard 的开放 JSON 只在此薄转换解析，不放宽治理的静态类型；
连接、授权、预算、Action 持久化和批准均复用已有机制。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from mcp.types import CallToolResult, TextContent
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
    from xiaowei.actions import ActionBinding, ActionPlan
    from xiaowei.mcp import MCPIntegration

D7_SCHEMAS = {
    "get_dashboard_by_uid": "4f22430350dbc608ec03519f30449b4265a4cabc4709d2f7dceecf1f1e9619cc",
    "list_dashboard_versions": "dec496ffd3e6175e964c73ff8f6853ca2102356a93eb94ea983fa2ca5889bde9",
    "update_dashboard": "714f031b72467e21aea0dda0b868f5049bf15fa0be39c7919b3fcbcf17ba2a43",
}
D7_POLICIES = frozenset(f"grafana.{n}" for n in D7_SCHEMAS)
_UID = re.compile(r"[a-zA-Z0-9_-]{1,40}")
_DASHBOARD_BYTES = 16_000


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DashboardArgs(_Args):
    dashboard_json: str
    folder_uid: str
    message: str


class DashboardWriteArgs(DashboardArgs):
    mode: Literal["create", "update"]


class DashboardReadArgs(_Args):
    uid: str
    version: int = Field(ge=0)


class DashboardVersionsArgs(_Args):
    uid: str
    limit: int = Field(ge=1)
    start: int = Field(ge=0)


class DashboardResult(_Args):
    result_json: str


class DashboardMissingError(Exception):
    """锁版当前对象读取明确不存在；仅供新建准备使用，不能交给模型冒充成功。"""


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _same_json(left: object, right: object) -> bool:
    """JSON数值允许上游规范化1.0为1，bool/字符串及嵌套结构必须严格区分。"""
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _same_json(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _same_json(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def schema_matches(name: str, schema: dict[str, object]) -> bool:
    shape = schema_shape(schema, ignore_extra_flags=True)
    digest = hashlib.sha256(_json(shape).encode()).hexdigest()
    return D7_SCHEMAS.get(name) == digest


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(_value: str) -> None:
    raise ValueError("non-finite JSON value")


def _decode(text: str) -> dict[str, Any] | list[dict[str, Any]]:
    value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_nonfinite)
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    raise ValueError("expected JSON object or object array")


def _body(text: object) -> dict[str, Any]:
    if not isinstance(text, str) or len(text.encode()) > _DASHBOARD_BYTES:
        raise ToolRejectedError("Dashboard 完整定义超过16000字节", code="capacity_exceeded")
    try:
        body = _decode(text)
        if not isinstance(body, dict):
            raise ValueError
        uid = body.get("uid")
        if not isinstance(uid, str) or not _UID.fullmatch(uid):
            raise ValueError
        if not isinstance(body.get("title"), str) or not body["title"].strip():
            raise ValueError
        if not isinstance(body.get("panels"), list) or any(
            name in body
            for name in ("elements", "layout", "apiVersion", "kind", "metadata", "spec")
        ):
            raise ValueError
        _json(body)
        return body
    except (ValueError, TypeError, RecursionError):
        raise ToolRejectedError("须为经典 panels[] 完整JSON，含有效uid/title") from None


def prepare_dashboard(request: ToolRequest) -> ToolRequest:
    args = request.arguments
    if "dashboard_json" in args:
        _body(args["dashboard_json"])
        folder, message = args["folder_uid"], args["message"]
        if not isinstance(folder, str) or (folder and not _UID.fullmatch(folder)):
            raise ToolRejectedError("文件夹UID不符合Grafana格式")
        if not isinstance(message, str) or not 1 <= len(message) <= 500:
            raise ToolRejectedError("版本说明须为1至500字符")
    else:
        uid = args["uid"]
        if not isinstance(uid, str) or not _UID.fullmatch(uid):
            raise ToolRejectedError("Dashboard UID不符合Grafana格式")
        if "limit" in args and args["limit"] != 1:
            raise ToolRejectedError("版本读取仅允许limit=1")
    return request


def wire_arguments(name: str, args: dict[str, object]) -> dict[str, object]:
    if name == "update_dashboard":
        body = _body(args["dashboard_json"])
        return {
            "dashboard": body,
            "folderUid": args["folder_uid"],
            "message": args["message"],
            "overwrite": False,
        }
    if name == "get_dashboard_by_uid":
        return {"uid": args["uid"], **({"version": args["version"]} if args["version"] else {})}
    return {"uid": args["uid"], "limit": 1, **({"start": args["start"]} if args["start"] else {})}


def _missing(text: str, uid: object) -> bool:
    prefix = f'get dashboard "{uid}" via k8s api: kubernetes API error: 404 Not Found (HTTP 404): '
    if not text.startswith(prefix):
        return False
    try:
        detail = _decode(text[len(prefix) :])
        if not isinstance(detail, dict):
            return False
        return bool(
            detail["code"] == 404
            and detail["status"] == "Failure"
            and detail["reason"] == "NotFound"
            and detail["details"]["name"] == uid
            and detail["details"]["group"] == "dashboard.grafana.app"
            and detail["details"]["kind"] == "dashboards"
        )
    except (ValueError, TypeError, KeyError):
        return False


def _version(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("Dashboard版本须为正整数")
    return value


def wire_payload(result: CallToolResult, name: str, args: dict[str, object]) -> dict[str, object]:
    from xiaowei.actions import ActionWriteRejectedError

    if (
        result.result_type != "complete"
        or result.structured_content is not None
        or len(result.content) != 1
        or not isinstance(result.content[0], TextContent)
    ):
        raise ValueError("Dashboard MCP结果协议不符")
    text = result.content[0].text
    if result.is_error:
        if name == "get_dashboard_by_uid" and not args["version"] and _missing(text, args["uid"]):
            raise DashboardMissingError()
        if name == "update_dashboard" and text == (
            "unable to save dashboard: [POST /dashboards/db] postDashboard (status 409): {}"
        ):
            raise ActionWriteRejectedError()
        raise ValueError("Dashboard MCP调用失败")
    value = _decode(text)
    if name == "get_dashboard_by_uid":
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("dashboard"), dict)
            or not isinstance(value.get("meta"), dict)
        ):
            raise ValueError("Dashboard对象不符")
        if type(value.get("isV2")) is not bool:
            raise ValueError("Dashboard格式标记类型不符")
        if not value["isV2"] and value["dashboard"].get("uid") != args["uid"]:
            raise ValueError("Dashboard读取UID不符")
        if args["version"]:
            if (
                _version(value["dashboard"].get("version")) != args["version"]
                or _version(value["meta"].get("version")) != args["version"]
            ):
                raise ValueError("Dashboard读取保存版本不符")
        if "folderUid" in value["meta"] and not isinstance(value["meta"]["folderUid"], str):
            raise ValueError("Dashboard读取文件夹类型不符")
        # 丢弃作者/组织返回的其他 metadata，仅保存定义和版本绑定所需信息。
        value = {
            "dashboard": value["dashboard"],
            "meta": {k: v for k, v in value["meta"].items() if k in ("folderUid", "version")},
            "isV2": value.get("isV2"),
        }
    elif name == "list_dashboard_versions":
        if not isinstance(value, list) or len(value) > 1:
            raise ValueError("Dashboard版本结果不符")
        value = [{"version": _version(item["version"])} for item in value]
        if args["start"] and any(item["version"] > args["start"] for item in value):
            raise ValueError("Dashboard版本范围不符")
    else:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("uid"), str)
            or value.get("status") != "success"
        ):
            raise ValueError("Dashboard写结果不符")
        _version(value.get("version"))
    return {"result_json": _json(value)}


def dashboard_catalog(
    configs: Iterable[MCPServerConfig],
    limits: dict[Audience, int],
) -> tuple[tuple[ToolContract, ...], tuple[ToolPolicy, ...]]:
    from xiaowei.actions import proposal_policy

    sources = [s for s in configs if any(p in D7_POLICIES for p in s.allowed_tools.values())]
    if not sources:
        return (), ()
    descriptions = {
        "get_dashboard_by_uid": (
            "读取完整Dashboard定义供编辑/恢复；version=0取当前，正整数取保存版本。原生v2"
            "只读。结果不是实时指标，历史不是当前。"
        ),
        "list_dashboard_versions": (
            "读取此UID的保存版本。limit=1，start=0取最新；start正整数读取至该版本。"
        ),
        "update_dashboard": (
            "仅供确定性群批准执行完整经典JSON；overwrite固定false，不进入Runner。"
        ),
        "propose_create_dashboard": (
            "仅提出新建经典Dashboard，不执行。dashboard_json为完整JSON文本，含ui"
            "d/title/panels[]；可按需求选择新uid和已授权文件夹，folder_uid空串为"
            "根目录。不要含凭据。message说明用途。群内完整展示后等本次批准。原生v2不可写。"
        ),
        "propose_update_dashboard": (
            "仅提出修改经典Dashboard，不执行。先按需取得完整定义，保留无需改变的字段；dashboa"
            "rd_json为含uid/title/panels[]的完整新定义。代码读取真实版本并生成差异/"
            "恢复材料，不用模型版本。folder_uid须显式指定，空串会放到根目录；message说明用途"
            "。原生v2只读，等待本次群批准。"
        ),
    }
    models: dict[str, type[BaseModel]] = {
        "get_dashboard_by_uid": DashboardReadArgs,
        "list_dashboard_versions": DashboardVersionsArgs,
        "update_dashboard": DashboardWriteArgs,
    }
    policies = [
        ToolPolicy(
            f"grafana.{name}",
            args,
            {a: Projection(("result_json",), limits[a]) for a in AUDIENCES},
            result=DashboardResult,
            required=("result_json",),
            effect="write" if name == "update_dashboard" else "read",
            group_only=name == "update_dashboard",
            fact_note="Dashboard定义/保存版本快照，不是实时指标；写结果只在确认回读后交付。",
        )
        for name, args in models.items()
    ]
    names: dict[str, list[str]] = {}
    for s in sources:
        names[s.server_id] = [n for n, p in s.allowed_tools.items() if p in D7_POLICIES]
        if "update_dashboard" in names[s.server_id]:
            for action in ("create_dashboard", "update_dashboard"):
                name = f"propose_{action}"
                names[s.server_id].append(name)
                policy = proposal_policy(f"grafana.{name}", DashboardArgs, min(limits.values()))
                if not any(p.policy_id == policy.policy_id for p in policies):
                    policies.append(policy)
    by_id = {p.policy_id: p for p in policies}
    contracts = tuple(
        ToolContract(
            tool_id=s.tool_id(name),
            target_id=s.server_id,
            policy_id=f"grafana.{name}",
            input_schema=by_id[f"grafana.{name}"].arguments.model_json_schema(),
            description=descriptions[name],
        )
        for s in sources
        for name in names[s.server_id]
    )
    selected = {c.policy_id for c in contracts}
    return contracts, tuple(p for p in policies if p.policy_id in selected)


def dashboard_actions(
    mcp: MCPIntegration,
    configs: Iterable[MCPServerConfig],
    catalog: ToolCatalog,
    *,
    clock: Callable[[], datetime],
) -> tuple[ActionBinding, ...]:
    from xiaowei.actions import ActionBinding, ActionPlan

    bindings: list[ActionBinding] = []
    for source in configs:
        if source.allowed_tools.get("update_dashboard") != "grafana.update_dashboard":
            continue
        sid = source.server_id

        async def read(
            name: str,
            args: dict[str, object],
            s: MCPServerConfig = source,
        ) -> dict[str, Any] | list[dict[str, Any]]:
            req = ToolRequest(
                tool_id=s.tool_id(name),
                target_id=s.server_id,
                call_id="dashboard-prepare",
                tool_name=s.sdk_tool_name(name),
                arguments=args,
            )
            execute = mcp.execute_for(req.tool_id, req.target_id)
            value = await (
                execute.run(execute.check(req)) if isinstance(execute, Prechecked) else execute(req)
            )
            return _decode(str(value.payload["result_json"]))

        async def snapshot(
            uid: str, load: Callable[..., Any] = read
        ) -> tuple[dict[str, Any], str, int]:
            current = await load("get_dashboard_by_uid", {"uid": uid, "version": 0})
            if current["isV2"] is not False:
                raise ProposalRejectedError("原生v2只读", code="object_unavailable")
            rows = await load("list_dashboard_versions", {"uid": uid, "limit": 1, "start": 0})
            if len(rows) != 1 or type(rows[0]["version"]) is not int or rows[0]["version"] < 1:
                raise ValueError("Dashboard保存版本不可确定")
            version = rows[0]["version"]
            saved = await load("get_dashboard_by_uid", {"uid": uid, "version": version})
            body = saved["dashboard"]
            if (
                body.get("uid") != uid
                or body.get("version") != version
                or saved["meta"].get("version") != version
            ):
                raise ValueError("Dashboard历史身份或版本不符")
            _body(_json(body))
            # 锁版MCP的FolderUID空值omitempty：根目录会省略，非空文件夹来自当前注解。
            folder = current["meta"].get("folderUid", "")
            if not isinstance(folder, str):
                raise ValueError("Dashboard文件夹不符")
            return body, folder, version

        async def plan(
            req: ToolRequest,
            load: Callable[..., Any] = read,
            snap: Callable[..., Any] = snapshot,
        ) -> ActionPlan:
            args = req.arguments
            body = _body(args["dashboard_json"])
            mode = args.get("mode") or (
                "create" if req.tool_id.endswith("propose_create_dashboard") else "update"
            )
            old: dict[str, Any] | None = None
            previous_folder = ""
            if mode == "create":
                try:
                    await load("get_dashboard_by_uid", {"uid": body["uid"], "version": 0})
                except DashboardMissingError:
                    version = 0
                else:
                    raise ProposalRejectedError(
                        "UID已存在，不能新建覆盖", code="object_unavailable"
                    )
            else:
                old, previous_folder, version = await snap(body["uid"])
            # 模型传来的id/version不是事实。id由已读历史决定，版本由代码绑定。
            body.pop("id", None)
            if old is not None and "id" in old:
                body["id"] = old["id"]
            body["version"] = version
            params = {
                "dashboard_json": _json(body),
                "folder_uid": args["folder_uid"],
                "message": args["message"],
                "mode": mode,
            }
            before = None if old is None else {"dashboard": old, "folderUid": previous_folder}
            after = {"dashboard": body, "folderUid": args["folder_uid"]}
            return ActionPlan(
                parameters=params,
                revision=arguments_digest(
                    {"before": before, "uid": body["uid"], "version": version}
                ),
                change=_json({"before": before, "after": after}),
                impact=(
                    f"仅此Dashboard {body['uid']}；完整替换定义，包含面板/变量/查询及文件夹。"
                    "数据源引用不授权查询其数据。"
                ),
                rollback=(
                    "新建后如需撤销，由Grafana管理员按UID人工删除并记录；首版无自动删除。"
                    if old is None
                    else (
                        "恢复需另次群审批，按最新版本重新提出以上差异的before.dashboard"
                        "完整原定义和before.folderUid原文件夹；不能直接重放旧版本。"
                    )
                ),
            )

        async def write(
            req: ToolRequest,
            snap: Callable[..., Any] = snapshot,
        ) -> ToolObservation:
            execute = mcp.execute_for(req.tool_id, req.target_id)
            result = await (
                execute.run(execute.check(req)) if isinstance(execute, Prechecked) else execute(req)
            )
            saved = _decode(str(result.payload["result_json"]))
            if not isinstance(saved, dict):
                raise ValueError("Dashboard保存结果不是对象")
            expected = _body(req.arguments["dashboard_json"])
            version = expected["version"] + 1
            if (
                saved.get("uid") != expected["uid"]
                or saved.get("status") != "success"
                or saved.get("version") != version
            ):
                raise ValueError("Dashboard写响应与批准不符")
            actual, folder, actual_version = await snap(expected["uid"])
            if actual_version != version or folder != req.arguments["folder_uid"]:
                raise ValueError("Dashboard回读版本/文件夹不符")
            # Grafana创建会补默认字段；所有明确批准的字段须精确一致。
            for key, value in expected.items():
                if key != "version" and not _same_json(actual.get(key), value):
                    raise ValueError("Dashboard回读定义不符")
            return ToolObservation(
                payload={
                    "result_json": _json(
                        {
                            "uid": expected["uid"],
                            "version": version,
                            "folderUid": folder,
                            "dashboard": actual,
                        }
                    )
                },
                captured_at=clock(),
                truncated=False,
            )

        def available(s: MCPServerConfig = source) -> bool:
            try:
                for name in D7_SCHEMAS:
                    mcp.execute_for(s.tool_id(name), s.server_id)
            except ToolRejectedError:
                return False
            return True

        def checked(req: ToolRequest, ready: Callable[[], bool] = available) -> ToolRequest:
            prepare_dashboard(req)
            if not ready():
                raise ToolRejectedError("Dashboard能力未核约或源不可用", code="unsupported")
            return req

        for action in ("create_dashboard", "update_dashboard"):
            proposal = catalog.contract(source.tool_id(f"propose_{action}"), sid)
            write_contract = catalog.contract(source.tool_id("update_dashboard"), sid)
            if proposal is None or write_contract is None:
                raise ValueError("Dashboard动作未登记")
            bindings.append(
                ActionBinding(
                    proposal,
                    write_contract,
                    plan,
                    Prechecked(prepare_dashboard, write),
                    target_binding=arguments_digest(
                        {
                            "config": source.model_dump(mode="json"),
                            "semantics": "classic-dashboard-v1",
                        }
                    ),
                    precheck=checked,
                    available=available,
                )
            )
    return tuple(bindings)
