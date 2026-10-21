"""受治理的 SDK FunctionTool：本地工具与 MCP 工具共用的薄包装。

只做三件事：把模型给出的参数解析为 JSON 对象，用 SDK 工具上下文中的调用标识构造
``ToolRequest``，经 ``GovernedTools.invoke`` 执行由应用绑定的 ``execute``。模型只能提供参数，
不能提供执行路径。

只有确定发生在 I/O 之前的治理拒绝（``ToolRejectedError``）按 SDK 公开的
``FunctionTool`` 返回固定结构反馈交给模型（不读取异常原文）。提出工具的只读准备可通过
专用的 ``ProposalRejectedError`` 返回明确拒绝：无 Action、无业务写入，已开始的调用计预算。
监控只读源的执行后失败由治理层记录后交回固定类别（分类不明的记 ``unclassified``），本源本轮不再
调用；其他执行后失败（非监控工具的结果不合约、证据无法保存、执行期间撤权或写动作结果不明）继续
抛出并中止整轮，不自动重试。

多目标工具（``routed_function_tool``）对模型只有一个函数，参数 ``cluster`` 必填：按它选取该目标
的契约与执行函数；缺少、不是字符串或没有登记该目标时以固定信息拒绝，不回退到其他目标。
目标与参数的一致性由 ``GovernedTools.invoke`` 再核对一次。
"""

import copy
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from agents import FunctionTool, UserError
from agents.tool_context import ToolContext

from xiaowei.governance import (
    TARGET_ARGUMENT,
    AlertmanagerCorrectionError,
    Execute,
    GovernedTools,
    GrafanaObjectMissingError,
    MonitoringReadError,
    PrometheusSelectorError,
    PromQLExpressionError,
    ProposalRejectedError,
    ToolRejectCode,
    ToolRejectedError,
    ToolRejectHint,
)
from xiaowei.models import ToolContract, ToolRequest, ToolResult
from xiaowei.sqlguard import REJECTION_MESSAGES, QueryRejectionCode

_UNKNOWN_CLUSTER = "目标未确认，未执行；请核实本轮明确目标，不猜选其他集群"
logger = logging.getLogger(__name__)

_REJECTION_FEEDBACK: dict[ToolRejectCode, tuple[str, bool]] = {
    "object_not_readable": (
        "对象不在可读的表结构中，未执行；可在已确认目标搜索或核实库表名，未匹配不证明不存在，不改查其他集群",
        True,
    ),
    "invalid_arguments": (
        "参数不符合工具契约或SQL写法不受支持；检查提供的参数schema和受支持写法，修改后再调用，不重发同一错误",
        True,
    ),
    "object_unavailable": (
        "该对象不能执行当前动作；未保存Action、未写入，请说明原因，不重试同一对象的该动作",
        False,
    ),
    "unsupported": ("该能力尚未开放，未执行；已停止该操作，告诉用户能力限制，不换参数绕过", False),
    "capacity_exceeded": (
        "结果或生成SQL超过固定容量限额，未执行；已停止该操作，告诉用户容量限制，不反复调用",
        False,
    ),
    "precheck_failed": ("工具前置检查失败，未执行；停止该操作，告诉用户当前无法执行", False),
    "unknown_target": (
        "目标未确认，未执行；核实用户在本会话明确选定的环境，不猜选集群；未选定先澄清",
        True,
    ),
    "not_allowed": (
        "本轮不允许使用该工具或当前无权调用该工具；停止该操作，不换身份、工具或源绕过",
        False,
    ),
    "authorization_unavailable": ("权限检查失败，未执行工具；停止该操作，不绕过权限复核", False),
    "budget_exhausted": ("本轮工具调用次数已用完；停止调用，用已有证据说明限制或澄清", False),
    "source_stopped": (
        "该监控源读取已失败或修正机会已用完；停止被拒绝的操作，可调查其他已授权源；"
        "PromQL 情形仍可按原契约读取发现信息",
        False,
    ),
    "schema_unavailable": (
        "该集群表结构暂不可用，未执行；本轮不要立即重试或等待刷新，不要改查其他集群；可后续重新提问",
        False,
    ),
    "busy": (
        "连接槽位繁忙，确认未发出任何SQL；仅可在本轮剩余配额、预算及期限内再试，配额为0时停止本集群",
        False,
    ),
}


_REJECTION_HINTS: dict[ToolRejectHint, str] = {
    "action_request": (
        "只有本轮已提出、证据可读且尚未请求过执行的Action ID可申请群审批；"
        "请核对ID并避免重复调用。同一轮须为执行请求和写入保留两次工具额度"
    ),
    "restart_cursor": (
        "cursor 无效、已失效或与本次参数不一致；请从头开始，cursor 传 null，再按返回的游标续取"
    ),
    "search_scope": (
        "下一个匹配对象的一行超过单值或结果字节上限；"
        "请用 database 过滤或更具体的关键词避开它，不重发同一搜索"
    ),
    "search_keyword": (
        "keyword 不能为空白；请给出有效关键词，列出全部表时使用 keyword=null 并指定 database"
    ),
    "database_required": ("列出全部表须指定 database；库名不明确先核实，列库请用 list_databases"),
    "page_size": "page_size 须为正整数且不超过工具说明中的页大小上限；修改后再调用",
    "audit_window": "时间窗须为正整数分钟且不超过工具说明中的审计窗口上限；请缩小或核实窗口",
    "audit_order": "order_by 只允许工具schema列出的排序方式；修改后再调用",
    "internal_ddl_only": (
        "原始 DDL 未读取：只支持普通内部表，视图和物化视图尚未开放；已停止该操作，告诉用户能力限制"
    ),
    "database_capacity": (
        "下一个库名超过结果容量，无法列出；已停止列库，告诉用户容量限制，不反复调用"
    ),
    "column_capacity": (
        "下一列的定义（含注释）超过单值或结果字节上限，无法显示；"
        "已停止读取字段结构，告诉用户容量限制"
    ),
    "ddl_sql_capacity": (
        "生成的 SHOW CREATE TABLE 超过 SQL 字节限额；已停止原始 DDL 读取，告诉用户容量限制"
    ),
    "change_promql": (
        "该表达式本轮已解析失败，未重发；修改括号、运算符或函数参数后再调用，不重发同一表达式"
    ),
    "change_selector": (
        "这组选择器本轮已被上游拒绝，换工具、label 或时间窗也不会重发；修改 matches 后再调用"
    ),
    "change_discovery_request": (
        "同一发现请求本轮已被上游拒绝，未重发；修改选择器或参数，或改用其他发现工具"
    ),
    "prometheus_text": "表达式/指标名最多8192字符，查询表达式不能为空；修改后再调用",
    "metric_required": (
        "metric 指标名不能为空；请提供具体指标，未知名称可用 label_values 按需发现"
    ),
    "selectors": "选择器最多32个、每个1–8192字符；series 至少一个选择器；修改后再调用",
    "label_name": "label 须为1–256字符的标签名（字母/下划线开头）；修改后再调用",
    "time_window": "起止时间须有序，窗口不超过31天；请核实起止时间或缩小窗口",
    "range_step": "step 至少1秒、每序列最多11000点；请增大步长或缩小窗口",
    "step_precision": "step 须为整毫秒且至少1秒；修改后再调用",
    "duration_format": "时间/步长须为有限秒数或单单位时长（如30s/5m）；修改后再调用",
    "timestamp_format": "时间须为Unix秒、含时区RFC3339、now或负的单单位时长；修改后再调用",
    "grafana_text": "查询文本最多2000字符，其他文本最多256字符，uid 不能为空；修改后再调用",
    "grafana_tags": "标签最多32个，每个1–256字符；修改后再调用",
    "grafana_pagination": (
        "limit 须为1–100；page 从1开始，offset/panelId 从0开始，不超过32位整数；修改后再调用"
    ),
    "grafana_time_window": "from/to 须为有序 Unix 毫秒，窗口不超过31天；请核实起止时间或缩小窗口",
    "silence_expired": "指定静默已到期，无需再取消；未保存Action、未写入，不对此ID再次提出取消",
    "silence_missing": (
        "指定静默ID在当前获准源未找到；未保存Action、未写入，请用户核对源或ID，不猜测或重发同一ID"
    ),
    "silence_filter": (
        "告警筛选条件被上游拒绝；未保存Action、未写入。"
        "核实匹配器名称、上游支持的Go正则和目标版本支持，修改后再提出，不重发同一条件"
    ),
    "silence_end": (
        "ends_at 须晚于当前时间与 starts_at；支持多年且必须明确到期，"
        "请核实用户要求的期限后修正，不要擅自改短"
    ),
    "datasource_fields": (
        "fields 含该插件不支持的键；只使用 allowed_fields 中的键修改后再提出，未保存Action、未写入"
    ),
    "datasource_field_type": (
        "fields 的字段值类型与插件说明不符；string 用字符串、boolean 用 true/false、number 用数字，"
        "修改后再提出，未保存Action、未写入"
    ),
    "datasource_no_schema": (
        "该插件没有字段说明；不要使用 fields，改用顶层 url、access、database 等参数，"
        "未保存Action、未写入"
    ),
    "alertmanager_change_request": (
        "本轮同一请求已失败，未重发；请修改 filters 或核对静默 ID，勿重复相同参数"
    ),
    "grafana_change_object": "指定 Dashboard UID 本轮已确认不存在；可核对 UID 或读取其他获准对象",
}


def _rejection_feedback(exc: ToolRejectedError, name: str) -> str:
    reason, retry = _REJECTION_FEEDBACK[exc.code]
    if exc.hint is not None:
        reason = _REJECTION_HINTS[exc.hint]
    feedback: dict[str, object] = {
        "status": "rejected",
        "code": exc.code,
        "reason": reason,
        "retry_this_turn": retry,
    }
    if exc.query_reject is not None:
        feedback["query_reject"] = exc.query_reject.value
        prefix = "执行计划未获取" if name == "explain_query" else "查询未执行"
        feedback["reason"] = (
            f"{prefix}（{exc.query_reject.value}）：{REJECTION_MESSAGES[exc.query_reject]}"
        )
        if exc.hint is not None:
            feedback["reason"] = f"{feedback['reason']}；{_REJECTION_HINTS[exc.hint]}"
    if exc.allowed_fields is not None:
        feedback["allowed_fields"] = exc.allowed_fields
    if exc.query_reject is QueryRejectionCode.UNQUALIFIED_TABLE:
        feedback.update(
            unqualified_tables=exc.unqualified_tables,
            missing_tables=exc.missing_tables,
            more=exc.more,
        )
    if exc.code == "busy":
        feedback.update(
            remaining_retries=exc.remaining_retries, retry_this_turn=exc.remaining_retries == 1
        )
    return json.dumps(feedback, ensure_ascii=False)


def governed_function_tool(
    name: str, contract: ToolContract, governance: GovernedTools, execute: Execute
) -> FunctionTool:
    """为已登记契约构造 SDK 函数工具；参数 schema 不受 SDK 严格模式支持时作为登记错误拒绝。"""

    def route(arguments: dict[str, Any]) -> tuple[ToolContract, Execute]:
        return contract, execute

    return _function_tool(name, contract, contract.description, governance, route)


def routed_function_tool(
    tool_id: str, governance: GovernedTools, executes: Mapping[str, Execute]
) -> FunctionTool:
    """多目标工具：每个登记目标一个执行函数，模型以参数 ``cluster`` 选择目标。

    函数名取 ``tool_id`` 的名字部分；说明在各目标相同时直接使用，不同时逐个目标列出。
    """
    catalog = governance.catalog
    contracts = {c.target_id: c for c in catalog.contracts_for(tool_id)}
    if not catalog.routed(tool_id) or set(executes) != set(contracts):
        raise ValueError(f"工具登记：{tool_id} 的目标与执行函数必须一一对应且声明 cluster")
    descriptions = {c.description for c in contracts.values()}
    if len(descriptions) == 1:
        description = descriptions.pop()
    else:
        description = "\n".join(f"集群 {t}：{c.description}" for t, c in contracts.items())
    choices = f"{TARGET_ARGUMENT} 必填，取值：{'、'.join(contracts)}。"
    description = f"{description}\n{choices}" if description else choices

    def route(arguments: dict[str, Any]) -> tuple[ToolContract, Execute]:
        cluster = arguments.get(TARGET_ARGUMENT)
        if not isinstance(cluster, str) or cluster not in contracts:
            raise ToolRejectedError(_UNKNOWN_CLUSTER, code="unknown_target")
        return contracts[cluster], executes[cluster]

    first = next(iter(contracts.values()))
    return _function_tool(tool_id.split("/", 1)[1], first, description, governance, route)


def _function_tool(
    name: str,
    schema_from: ToolContract,
    description: str,
    governance: GovernedTools,
    route: Callable[[dict[str, Any]], tuple[ToolContract, Execute]],
) -> FunctionTool:
    async def invoke(tool_ctx: ToolContext[Any], raw_arguments: str) -> str:
        try:
            arguments = json.loads(raw_arguments or "{}")
        except ValueError:
            arguments = None
        if not isinstance(arguments, dict):
            governance.note_rejected(tool_ctx.context.identity)
            return _rejection_feedback(ToolRejectedError("参数不符合工具契约"), name)
        try:
            contract, execute = route(arguments)
            result = await _governed(tool_ctx, contract, arguments, governance, execute)
        except ProposalRejectedError as exc:
            # 只读准备已计入预算/started；治理层确认仅提出工具可返回，不重复计数。
            return _rejection_feedback(exc, name)
        except ToolRejectedError as exc:
            governance.note_rejected(tool_ctx.context.identity)
            logger.info(
                "tool_rejected turn=%s call_ref=%s tool=%s code=%s",
                tool_ctx.context.identity.turn_id,
                hashlib.sha256(
                    tool_ctx.tool_call_id.encode("utf-8", errors="surrogatepass")
                ).hexdigest()[:16],
                name,
                exc.code,
            )
            return _rejection_feedback(exc, name)
        except PromQLExpressionError as exc:
            return json.dumps(
                {
                    "status": "invalid_expression",
                    "source": contract.target_id,
                    "line": exc.line,
                    "column": exc.column,
                    "correction_allowed": exc.correction_allowed,
                    "reason": "PromQL解析失败；检查括号、运算符与函数参数，勿重发同一表达式",
                },
                ensure_ascii=False,
            )
        except PrometheusSelectorError as exc:
            return json.dumps(
                {
                    "status": "invalid_selector",
                    "source": contract.target_id,
                    "correction_allowed": exc.correction_allowed,
                    "reason": (
                        "上游拒绝选择器或参数；matches 每项须是合法选择器且至少有一个匹配器"
                        '不匹配空串，例如 node_uname_info 或 {job="node"}，勿重发同一请求'
                    ),
                },
                ensure_ascii=False,
            )
        except AlertmanagerCorrectionError as exc:
            reasons = {
                "response_limit": (
                    "告警或静默列表响应超过接收上限；可加 filters 按 alertname、instance、"
                    "severity 或 job 收窄后重试"
                ),
                "invalid_matcher": (
                    "上游拒绝匹配条件；请检查 filters 的 matcher 或 receiver 正则，例如 "
                    'alertname="HostDown" '
                    '或 instance=~"10\\.0\\..*"'
                ),
                "silence_missing": "未找到该静默 ID；请核对 ID 或从已授权源读取静默目录",
            }
            return json.dumps(
                {
                    "status": exc.reason,
                    "source": contract.target_id,
                    "correction_allowed": exc.correction_allowed,
                    "reason": reasons[exc.reason],
                },
                ensure_ascii=False,
            )
        except GrafanaObjectMissingError as exc:
            return json.dumps(
                {
                    "status": "object_missing",
                    "source": contract.target_id,
                    "correction_allowed": exc.correction_allowed,
                    "reason": "指定 Dashboard UID 未找到；可核对 UID 或读取其他获准对象",
                },
                ensure_ascii=False,
            )
        except MonitoringReadError as exc:
            return json.dumps(
                {
                    "status": "unverified",
                    "source": contract.target_id,
                    "reason": exc.reason,
                    "retry_this_turn": False,
                },
                ensure_ascii=False,
            )
        return result.model_content

    try:
        return FunctionTool(
            name=name,
            description=description,
            params_json_schema=copy.deepcopy(schema_from.input_schema),
            on_invoke_tool=invoke,
            strict_json_schema=True,
        )
    except UserError:
        raise ValueError(
            f"工具登记：{schema_from.tool_id} 的参数 schema 不受 SDK 严格模式支持"
        ) from None


def _governed(
    tool_ctx: ToolContext[Any],
    contract: ToolContract,
    arguments: dict[str, Any],
    governance: GovernedTools,
    execute: Execute,
) -> Awaitable[ToolResult]:
    request = ToolRequest(
        tool_id=contract.tool_id,
        target_id=contract.target_id,
        call_id=tool_ctx.tool_call_id,
        tool_name=tool_ctx.tool_name,
        arguments=arguments,
    )
    return governance.invoke(tool_ctx.context, request, execute)
