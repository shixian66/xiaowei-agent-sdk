"""数据库工作方法（两份 Skill）与环境规则送达模型的离线契约。

只证明模型可见的文字齐全、没有旧的跨环境发现规则；不代表模型会遵守，遵守情况由获准的真实
模型评测判定。
"""

from importlib.resources import files
from typing import get_args

import pytest
from tests.p1b.test_starrocks_tool_contracts import AUDIENCES, adapter, unrefreshed

from xiaowei.app import (
    DEFAULT_INSTRUCTIONS,
    MONITORING_INSTRUCTIONS,
    TargetInfo,
    _instructions,
)
from xiaowei.evidence import _policy_fingerprint
from xiaowei.governance import ToolRejectCode, ToolRejectedError, ToolRejectHint
from xiaowei.starrocks_tools import starrocks_tools
from xiaowei.tools import _rejection_feedback

SKILLS = ("database_query.md", "database_diagnosis.md")
CLUSTERS = ("fat", "pre")
TARGETS = tuple(
    TargetInfo(target_id=t, description=f"合成集群 {t}", business_context=None) for t in CLUSTERS
)
CAPABILITIES = {t: ["local/describe_table", "local/list_tables"] for t in CLUSTERS}
# 旧 R7 B1(a) 允许未选环境时跨环境发现并按“唯一匹配”继续读取，是事故的规则来源。
LEGACY = (
    "获准发现",
    "获准候选",
    "唯一可读",
    "发现不完整",
    "穷举发现",
    "完整发现",
)
# 每条环境规则只出现一次：重复的说法容易在改写时互相冲突。
ENVIRONMENT_RULES = (
    "只用用户在本会话明确选定的环境",
    "不做跨环境发现",
    "只配置一个环境时也先确认",
    "本会话后续数据库任务沿用该环境",
    "只有用户明确要求切换",
    "本轮请求明确指定单个环境取数或查结构",
    "只是提到别的环境",
    "先问是否切换",
    "按点名范围查询",
    "其他成员要换环境同样须明确切换",
    "执行许可不继承",
    "无法从对话确定当前环境时重新询问",
    "新建会话后重新询问",
    "不改查其他环境代替",
    "不要求选环境",
    "选择环境不授予权限",
)


def skill(name: str) -> str:
    return files("xiaowei").joinpath("skills", name).read_text(encoding="utf-8")


def assembled() -> str:
    return _instructions(DEFAULT_INSTRUCTIONS, TARGETS, CAPABILITIES)


def tool_contracts():
    ada = adapter()
    return starrocks_tools(ada, dict.fromkeys(AUDIENCES, 400_000), schema=unrefreshed(ada))


def rejection_reasons() -> list[str]:
    reasons = [
        _rejection_feedback(ToolRejectedError("x", code=code), "any_tool")
        for code in get_args(ToolRejectCode)
    ]
    reasons += [
        _rejection_feedback(ToolRejectedError("x", hint=hint), "any_tool")
        for hint in get_args(ToolRejectHint)
    ]
    return reasons


@pytest.mark.parametrize("name", SKILLS)
def test_both_skills_reach_the_model_when_starrocks_targets_exist(name: str) -> None:
    body = skill(name)
    assert body.strip() and len(body) <= 3000
    assert body.strip() in assembled()
    assert body.strip() not in DEFAULT_INSTRUCTIONS
    assert body.strip() not in _instructions(DEFAULT_INSTRUCTIONS, (), {})


def test_target_header_lists_clusters_without_repeating_rules() -> None:
    text = assembled()
    for target in CLUSTERS:
        assert f"- {target}：合成集群 {target}" in text
    assert "列出这些集群不代表用户已选择" in text
    # 规则只在查询 Skill 中出现一次，目标清单不再各写一遍。
    assert text.count("先澄清，不要猜测") == 0


def test_every_model_visible_text_drops_cross_environment_discovery() -> None:
    texts = {"instructions": assembled(), "monitoring": MONITORING_INSTRUCTIONS}
    tools = tool_contracts()
    texts |= {c.tool_id: c.description for c in tools.contracts}
    texts |= {f"reject-{i}": r for i, r in enumerate(rejection_reasons())}
    for where, text in texts.items():
        for phrase in LEGACY:
            assert phrase not in text, (where, phrase)


@pytest.mark.parametrize("rule", ENVIRONMENT_RULES)
def test_environment_rules_are_stated_exactly_once(rule: str) -> None:
    assert assembled().count(rule) == 1


def test_every_starrocks_tool_requires_the_session_selected_environment() -> None:
    tools = tool_contracts()
    assert tools.contracts
    for contract in tools.contracts:
        assert "集群须是用户在本会话明确选定的环境，未选定时先澄清" in contract.description
        assert "不换集群绕过拒绝" in contract.description


def test_tool_description_change_keeps_existing_evidence_fingerprints() -> None:
    tools = tool_contracts()
    policies = {p.policy_id: p for p in tools.policies}
    for contract in tools.contracts:
        policy = policies[contract.policy_id]
        reworded = contract.model_copy(update={"description": "说明文字改写"})
        assert _policy_fingerprint(reworded, policy) == _policy_fingerprint(contract, policy)


def test_security_skill_is_short_and_only_loaded_for_registered_security_capability() -> None:
    body = skill("database_security.md").strip()
    assert 0 < len(body) <= 1800
    capabilities = {t: list(CAPABILITIES[t]) for t in CLUSTERS}
    capabilities["pre"].append("local/inspect_audit_security")
    assert body in _instructions(DEFAULT_INSTRUCTIONS, TARGETS, capabilities)
    assert body not in assembled()
    assert "专用安全巡检能力未登记" in assembled()
    assert body not in _instructions(DEFAULT_INSTRUCTIONS, (), {})
    for rule in ("match_counts_complete", "已确认下界", "不相加", "不固定", "EXPLAIN", "正常"):
        assert rule in body
