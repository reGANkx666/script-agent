"""图路由函数。

输入：状态。
输出：下一个节点名。
作用：让路由只依赖模型选择的动作和明确缺失字段。
实现：纯函数；预算与安全护栏在 agent 节点中完成，不在这里写业务分支。
"""

from __future__ import annotations

from src.domain.status import ActionName
from src.graph.state import AgentState


def route_after_validate(state: AgentState) -> str:
    """输入：校验后状态；输出：ask_user 或 agent；作用：缺信息先询问。"""
    return "ask_user" if state.get("missing_fields") else "agent"


def route_after_agent(state: AgentState) -> str:
    """输入：Agent 状态；输出：动作节点名；作用：驱动条件边；实现：只读 pending_action。"""
    action = state.get("pending_action")
    if action is None:
        return "fail"
    return str(action.action)


def route_after_ask(state: AgentState) -> str:
    """输入：ask_user 后状态；输出：仍缺信息则继续询问，否则进入 Agent。"""
    return "ask_user" if state.get("missing_fields") else "agent"


def is_terminal_action(action: ActionName) -> bool:
    """输入：动作；输出：是否直接结束；作用：构建图和测试断言共用。"""
    return action in {ActionName.FINALIZE, ActionName.FAIL}
