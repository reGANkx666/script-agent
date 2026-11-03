"""输入校验节点。"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from src.domain.models import Brief
from src.graph.nodes.common import apply_defaults, emit, required_missing
from src.graph.services import GraphServices
from src.graph.state import AgentState


def make_validate_node(services: GraphServices):
    """输入：依赖容器；输出：validate_input 节点；作用：补齐可默认字段并计算缺失项。"""

    async def validate_input(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        """校验 Brief。

        输入：状态。
        输出：brief/risks/missing_fields/status 更新。
        作用：决定进入 ask_user 还是 agent，绝不猜测 platform 或目标时长。
        实现：必填项独立计算；audience/style 缺省时补默认并写入 risks。
        """
        brief = state.get("brief")
        risks = list(state.get("risks", []))
        missing = required_missing(brief)
        if brief is not None:
            brief, risks = apply_defaults(brief, risks)
        await emit(
            config,
            "tool.result",
            "validate_input",
            {"missing_fields": missing, "risk_codes": [risk.get("code") for risk in risks]},
        )
        return {
            "brief": brief,
            "risks": risks,
            "missing_fields": missing,
            "status": "RUNNING",
            "pending_action": None,
            "last_observation": {"action": "validate_input", "missing_fields": missing},
        }

    return validate_input


def validate_brief(brief: Any) -> list[str]:
    """输入：任意 Brief 载荷；输出：缺失字段；作用：API 测试与图外调用复用。"""
    if isinstance(brief, Brief):
        return required_missing(brief)
    return ["topic", "platform", "target_duration_sec"]
