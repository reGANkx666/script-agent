"""素材检索节点。"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.runnables import RunnableConfig

from src.domain.fact_ledger import claims_from_materials
from src.domain.models import Material, SearchResult
from src.graph.nodes.common import emit, merge_materials
from src.graph.services import GraphServices
from src.graph.state import AgentState


def make_search_node(services: GraphServices):
    """输入：依赖容器；输出：search_materials 节点。"""

    async def search_materials(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        """执行一次检索。

        输入：pending_action 中的 query/top_k。
        输出：materials、claims、rewrite_count、tool_calls 和观察。
        作用：为 Writer 建立事实白名单；EMPTY/ERROR 必须原样暴露。
        实现：调用注入的检索工具；命中素材去重合并；不在此处编造 claim。
        """
        action = state.get("pending_action")
        args = action.args if action else {}
        query = str(args.get("query", "")).strip()
        top_k = int(args.get("top_k", 5) or 5)
        tool_calls = int(state.get("tool_calls", 0)) + 1
        queries = list(state.get("searched_queries", []))
        if query:
            queries.append(query)

        await emit(config, "tool.called", "search_materials", {"query": query, "top_k": top_k})
        try:
            result = await asyncio.wait_for(
                services.retrieval.search_materials(query, top_k=top_k),
                timeout=services.settings.tool_timeout_seconds,
            )
        except Exception as exc:
            result = SearchResult(
                status="ERROR",
                query=query,
                error=f"检索工具异常：{type(exc).__name__}",
                debug={"error_type": type(exc).__name__},
            )

        incoming = [
            Material(
                id=hit.source_id,
                text=hit.text,
                source=hit.source,
                source_type=hit.source_type,
                tags=[],
            )
            for hit in result.results
            if hit.source_id and hit.text
        ]
        materials = merge_materials(list(state.get("materials", [])), incoming)
        claims = claims_from_materials(materials, list(state.get("claims", [])))
        contexts = _merge_contexts(list(state.get("contexts", [])), list(result.contexts))
        risks = list(state.get("risks", []))
        if result.risk and result.risk not in risks:
            risks.append(result.risk)
        rewrite_count = int(state.get("rewrite_count", 0))
        if result.status in {"EMPTY", "LOW_SCORE"}:
            rewrite_count += 1

        observation = {
            "action": "search_materials",
            "query": query,
            "status": result.status,
            "source_ids": [material.id for material in incoming],
            "result_count": len(result.results),
            "context_count": len(contexts),
            "rerank_status": result.debug.get("rerank_status", "disabled"),
            "rewrite_count": rewrite_count,
            "error": result.error,
            "error_status": "TOOL_ERROR" if result.status == "ERROR" else None,
        }
        await emit(
            config,
            "tool.result",
            "search_materials",
            {
                "status": result.status,
                "query": query,
                "source_ids": observation["source_ids"],
                "result_count": len(result.results),
                "context_count": observation["context_count"],
                "rerank_status": observation["rerank_status"],
                "error": result.error,
            },
        )
        return {
            "materials": materials,
            "claims": claims,
            "contexts": contexts,
            "risks": risks,
            "tool_calls": tool_calls,
            "rewrite_count": rewrite_count,
            "searched_queries": queries,
            "pending_action": None,
            "last_observation": observation,
        }

    return search_materials


def _merge_contexts(existing, incoming):
    """输入：已有/新增 Parent 上下文；输出：按 parent_id 去重的列表。"""
    by_id = {context.parent_id: context for context in existing}
    for context in incoming:
        if context.parent_id not in by_id:
            by_id[context.parent_id] = context
            continue
        current = by_id[context.parent_id]
        for source_id in context.source_ids:
            if source_id not in current.source_ids:
                current.source_ids.append(source_id)
    return list(by_id.values())
