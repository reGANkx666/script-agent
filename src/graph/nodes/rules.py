"""规则检查节点。"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.runnables import RunnableConfig

from src.graph.nodes.common import emit
from src.graph.services import GraphServices
from src.graph.state import AgentState


def make_rules_node(services: GraphServices):
    """输入：依赖容器；输出：check_rules 节点。"""

    async def check_rules(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        """执行品牌/平台规则。

        输入：draft、duration、claims、risks、平台。
        输出：check、risks、tool_calls 与观察。
        作用：阻断无来源事实、禁语和超时长；block 违规不可 SUCCESS。
        实现：异步加载规则快照，再调用确定性规则函数；工具失败显式记录。
        """
        draft = state.get("draft")
        duration = state.get("duration")
        brief = state["brief"]
        tool_calls = int(state.get("tool_calls", 0)) + 1
        if draft is None or duration is None:
            observation = {
                "action": "check_rules",
                "status": "ERROR",
                "error": "缺少 draft 或 duration",
                "error_status": "TOOL_ERROR",
            }
            await emit(config, "tool.result", "check_rules", observation)
            return {
                "tool_calls": tool_calls,
                "pending_action": None,
                "last_observation": observation,
            }

        await emit(config, "tool.called", "check_rules", {"platform": brief.platform})
        try:
            snapshot = await asyncio.wait_for(
                services.rule_provider.get_rules(brief.platform),
                timeout=services.settings.tool_timeout_seconds,
            )
            report = services.rule_checker(
                draft,
                brief,
                list(state.get("materials", [])),
                snapshot.brand,
                snapshot.platform,
                duration=duration,
                claims=list(state.get("claims", [])),
                risks=list(state.get("risks", [])),
            )
        except Exception as exc:
            observation = {
                "action": "check_rules",
                "status": "ERROR",
                "error": f"规则工具失败：{type(exc).__name__}",
                "error_status": "TOOL_ERROR",
            }
            await emit(
                config,
                "tool.result",
                "check_rules",
                {"status": "ERROR", "error_type": type(exc).__name__},
            )
            return {
                "tool_calls": tool_calls,
                "pending_action": None,
                "last_observation": observation,
            }

        risks = _merge_risk_items(list(state.get("risks", [])), report.violations)
        observation = {
            "action": "check_rules",
            "status": "OK",
            "passed": report.passed,
            "violation_codes": [item.code for item in report.violations],
            "block_count": sum(1 for item in report.violations if item.severity == "block"),
        }
        await emit(config, "tool.result", "check_rules", observation)
        return {
            "check": report,
            "risks": risks,
            "tool_calls": tool_calls,
            "pending_action": None,
            "last_observation": observation,
        }

    return check_rules


def _merge_risk_items(risks: list[dict[str, Any]], violations) -> list[dict[str, Any]]:
    """输入：已有风险与违规；输出：去重风险；作用：让最终结果展示 warn/info。

    实现：block 只保留在 check report；warn/info 转成 risk 条目，相同 code/shot 去重。
    """
    merged = list(risks)
    seen = {(item.get("code"), item.get("shot_id"), item.get("field")) for item in merged}
    for violation in violations:
        if violation.severity == "block":
            continue
        key = (violation.code, violation.shot_id, None)
        if key in seen:
            continue
        seen.add(key)
        merged.append(
            {
                "code": violation.code,
                "severity": violation.severity,
                "shot_id": violation.shot_id,
                "message": violation.message,
            }
        )
    return merged
