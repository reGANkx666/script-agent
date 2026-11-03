"""LangGraph 状态。

输入：节点返回的部分更新。
输出：贯穿 validate → agent → tools → finalize 的可序列化状态。
作用：只保存业务状态，不把全量事件/消息历史塞进图。
实现：TypedDict；默认覆盖语义，由节点显式返回完整列表/对象。
"""

from __future__ import annotations

from typing import Any, TypedDict

from src.domain.models import (
    ActionDecision,
    Brief,
    CheckReport,
    Claim,
    ContextBlock,
    DurationReport,
    Material,
    Script,
)


class RunCancelled(RuntimeError):
    """运行在节点边界被取消。"""


class AgentState(TypedDict, total=False):
    """Agent 图的共享状态。"""

    run_id: str
    brief: Brief
    materials: list[Material]
    claims: list[Claim]
    contexts: list[ContextBlock]
    draft: Script | None
    duration: DurationReport | None
    check: CheckReport | None
    risks: list[dict[str, Any]]
    status: str
    current_node: str
    step: int
    tool_calls: int
    rewrite_count: int
    revision_count: int
    pending_question: dict[str, Any] | None
    pending_action: ActionDecision | None
    last_observation: dict[str, Any] | None
    result: dict[str, Any] | None
    missing_fields: list[str]
    searched_queries: list[str]
    error: dict[str, Any] | None


def initial_state(run_id: str, brief: Brief, status: str = "QUEUED") -> AgentState:
    """构造初始状态。

    输入：run_id、Brief、初始状态。
    输出：所有集合字段已初始化的 AgentState。
    作用：避免节点对 None/list 做重复兼容。
    实现：显式给出空集合与计数器。
    """
    return AgentState(
        run_id=run_id,
        brief=brief,
        materials=[],
        claims=[],
        contexts=[],
        draft=None,
        duration=None,
        check=None,
        risks=[],
        status=status,
        current_node="",
        step=0,
        tool_calls=0,
        rewrite_count=0,
        revision_count=0,
        pending_question=None,
        pending_action=None,
        last_observation=None,
        result=None,
        missing_fields=[],
        searched_queries=[],
        error=None,
    )
