"""时长估算节点。"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from src.domain.duration_policy import sync_shot_durations
from src.graph.nodes.common import emit
from src.graph.services import GraphServices
from src.graph.state import AgentState


def make_duration_node(services: GraphServices):
    """输入：依赖容器；输出：estimate_duration 节点。"""

    async def estimate_duration(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        """估算当前脚本时长并回写分镜。

        输入：draft、Brief 目标时长。
        输出：duration、同步后的 draft、tool_calls 与观察。
        作用：用纯函数判断是否满足 target ± tolerance。
        实现：调用注入估算器；把逐分镜估算写回 `Shot.duration_sec`，保证输出不变量。
        """
        draft = state.get("draft")
        brief = state["brief"]
        tool_calls = int(state.get("tool_calls", 0)) + 1
        if draft is None or brief.target_duration_sec is None:
            observation = {
                "action": "estimate_duration",
                "status": "ERROR",
                "error": "缺少 draft 或目标时长",
                "error_status": "TOOL_ERROR",
            }
            await emit(config, "tool.result", "estimate_duration", observation)
            return {
                "tool_calls": tool_calls,
                "pending_action": None,
                "last_observation": observation,
            }

        await emit(
            config,
            "tool.called",
            "estimate_duration",
            {"script_id": draft.script_id, "target_sec": brief.target_duration_sec},
        )
        try:
            report = await services.get_duration(draft, brief.target_duration_sec, 2)
            synced = sync_shot_durations(draft, report)
        except Exception as exc:
            observation = {
                "action": "estimate_duration",
                "status": "ERROR",
                "error": f"时长工具失败：{type(exc).__name__}",
                "error_status": "TOOL_ERROR",
            }
            await emit(
                config,
                "tool.result",
                "estimate_duration",
                {"status": "ERROR", "error_type": type(exc).__name__},
            )
            return {
                "tool_calls": tool_calls,
                "pending_action": None,
                "last_observation": observation,
            }

        observation = {
            "action": "estimate_duration",
            "status": "OK",
            "total_sec": report.total_sec,
            "target_sec": report.target_sec,
            "tolerance_sec": report.tolerance_sec,
            "passed": report.passed,
        }
        await emit(config, "tool.result", "estimate_duration", observation)
        return {
            "draft": synced,
            "duration": report,
            "check": None,
            "tool_calls": tool_calls,
            "pending_action": None,
            "last_observation": observation,
        }

    return estimate_duration
