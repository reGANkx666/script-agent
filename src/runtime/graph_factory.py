"""图工厂薄封装。

输入：节点依赖与 checkpointer。
输出：编译图。
作用：给 runtime 层提供稳定装配点，避免调用方直接依赖 graph.build。
实现：委托 `build_graph`，便于后续替换/包装。
"""

from __future__ import annotations

from typing import Any

from src.graph.build import build_graph
from src.graph.services import GraphServices


def build_runtime_graph(services: GraphServices, checkpointer: Any):
    """输入：GraphServices、checkpointer；输出：编译图；作用：生产容器装配。"""
    return build_graph(services, checkpointer)
