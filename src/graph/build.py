"""LangGraph 图构建。

输入：节点依赖与 checkpointer。
输出：编译后的 `StateGraph`。
作用：把确定性节点、模型决策和条件边装配成可暂停/恢复的运行图。
实现：节点外包一层事件与取消护栏；条件边只读 pending_action / missing_fields。
"""

from __future__ import annotations

import functools
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph

from src.graph.nodes.agent import make_agent_node
from src.graph.nodes.ask import make_ask_node
from src.graph.nodes.common import check_cancelled, emit
from src.graph.nodes.draft import make_draft_node
from src.graph.nodes.duration import make_duration_node
from src.graph.nodes.fail import make_fail_node
from src.graph.nodes.finalize import make_finalize_node
from src.graph.nodes.revise import make_revise_node
from src.graph.nodes.rules import make_rules_node
from src.graph.nodes.search import make_search_node
from src.graph.nodes.validate import make_validate_node
from src.graph.routing import route_after_agent, route_after_ask, route_after_validate
from src.graph.services import GraphServices
from src.graph.state import AgentState


def build_graph(services: GraphServices, checkpointer: Any):
    """构建并编译 LangGraph。

    输入：节点依赖与 checkpointer。
    输出：可 `ainvoke/astream` 的编译图。
    作用：生产使用 Postgres checkpointer，测试使用 MemorySaver。
    实现：注册节点、条件边与固定回边；`recursion_limit` 由运行配置控制。
    """
    builder = StateGraph(AgentState)
    raw_nodes = {
        "validate_input": make_validate_node(services),
        "agent": make_agent_node(services),
        "search_materials": make_search_node(services),
        "draft_script": make_draft_node(services),
        "estimate_duration": make_duration_node(services),
        "check_rules": make_rules_node(services),
        "revise_script": make_revise_node(services),
        "ask_user": make_ask_node(services),
        "finalize": make_finalize_node(services),
        "fail": make_fail_node(services),
    }
    for name, node in raw_nodes.items():
        builder.add_node(name, _wrap_node(name, node))

    builder.add_edge(START, "validate_input")
    builder.add_conditional_edges(
        "validate_input",
        route_after_validate,
        {"ask_user": "ask_user", "agent": "agent"},
    )
    builder.add_conditional_edges(
        "agent",
        route_after_agent,
        {
            "search_materials": "search_materials",
            "draft_script": "draft_script",
            "estimate_duration": "estimate_duration",
            "check_rules": "check_rules",
            "revise_script": "revise_script",
            "ask_user": "ask_user",
            "finalize": "finalize",
            "fail": "fail",
        },
    )
    for name in (
        "search_materials",
        "draft_script",
        "estimate_duration",
        "check_rules",
        "revise_script",
    ):
        builder.add_edge(name, "agent")
    builder.add_conditional_edges(
        "ask_user",
        route_after_ask,
        {"ask_user": "ask_user", "agent": "agent"},
    )
    builder.add_edge("finalize", END)
    builder.add_edge("fail", END)
    return builder.compile(checkpointer=checkpointer, name="script_agent")


def _wrap_node(name: str, node):
    """包装节点：取消检查 + node.started/completed 事件。

    输入：节点名与实现。
    输出：兼容 LangGraph `(state, config)` 签名的包装函数。
    作用：让每个节点边界都可取消、可观测。
    实现：异常发出 node.failed 后原样抛出；中断时不产生 completed。
    """

    @functools.wraps(node)
    async def wrapped(state: AgentState, config: RunnableConfig):
        await check_cancelled(config)
        # ask_user 在 interrupt 之前不写任何持久化事件；恢复后再记录 completed。
        if name != "ask_user":
            await emit(
                config,
                "node.started",
                name,
                {
                    "step": int(state.get("step", 0)),
                    "status": state.get("status"),
                },
            )
        try:
            update = await node(state, config)
        except GraphInterrupt:
            # 暂停不是失败；等待输入由 RunManager 根据快照记录。
            raise
        except Exception as exc:
            await emit(
                config,
                "node.failed",
                name,
                {"error_type": type(exc).__name__},
            )
            raise
        if isinstance(update, dict):
            update.setdefault("current_node", name)
            await emit(
                config,
                "node.completed",
                name,
                {
                    "updated_keys": sorted(update.keys()),
                    "status": update.get("status", state.get("status")),
                },
            )
        return update

    return wrapped
