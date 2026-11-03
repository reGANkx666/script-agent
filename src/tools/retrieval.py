"""混合检索工具：BM25 + pgvector + RRF。

输入：查询文本。
输出：显式状态的 `SearchResult`。
作用：为 Agent 提供可解释的素材召回；失败时返回 ERROR，绝不伪造结果或静默降级。
实现：两路各取 Top 20，向量相似度低于阈值丢弃，再用 RRF 融合。
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from src.config import Settings
from src.domain.models import ContextBlock, Material, SearchHit, SearchResult
from src.tools.bm25 import BM25Index
from src.tools.embeddings import EmbeddingClient
from src.tools.reranker import Reranker


class MaterialRepository(Protocol):
    """素材数据访问接口。"""

    async def list_active(self) -> list[Material]:
        """输出：active 素材；作用：构建 BM25 索引。"""
        ...

    async def vector_search(
        self, embedding: list[float], limit: int = 20, min_score: float = 0.25
    ) -> list[tuple[Material, float]]:
        """输入：查询向量；输出：(素材, 相似度)；作用：向量一路召回。"""
        ...


class RetrievalTool(Protocol):
    """检索工具最小接口。"""

    async def search_materials(
        self, query: str, top_k: int = 5, use_rerank: bool = True
    ) -> SearchResult:
        """输入：查询、Top-K、是否 rerank；输出：检索结果；作用：供图节点/评估调用。"""
        ...


class HybridRetrievalService:
    """BM25、向量与 RRF 组合检索。"""

    def __init__(
        self,
        repository: MaterialRepository,
        embedder: EmbeddingClient,
        settings: Settings,
        *,
        reranker: Reranker | None = None,
        rrf_k: int = 60,
        min_vector_score: float = 0.25,
        candidate_k: int | None = None,
    ) -> None:
        """输入：素材仓库、embedding、配置；输出：检索服务；作用：注入真实或测试依赖。"""
        self.repository = repository
        self.embedder = embedder
        self.settings = settings
        self.reranker = reranker
        self.rrf_k = rrf_k
        self.min_vector_score = min_vector_score
        self.candidate_k = candidate_k or max(settings.rerank_candidate_k, 30)
        self._index: BM25Index | None = None
        self._index_lock = asyncio.Lock()

    async def _get_index(self) -> BM25Index:
        """输入：无；输出：缓存的 BM25 索引；作用：避免每次查询重读素材；实现：加锁懒构建。"""
        if self._index is None:
            async with self._index_lock:
                if self._index is None:
                    materials = await self.repository.list_active()
                    self._index = BM25Index(materials)
        return self._index

    def invalidate_index(self) -> None:
        """输入：无；输出：无；作用：素材更新后强制下次重建。

        实现：清空缓存引用。
        """
        self._index = None

    async def search_materials(
        self, query: str, top_k: int = 5, use_rerank: bool = True
    ) -> SearchResult:
        """执行混合检索，失败重试一次。

        输入：查询文本、Top-K、是否使用 reranker。
        输出：OK/EMPTY/LOW_SCORE/ERROR 的检索结果。
        作用：所有调用方都能区分“没有素材”和“工具失败”；评估可关闭 rerank 做对照。
        实现：BM25 与向量并行；RRF 融合；任一外部依赖失败则整体 ERROR。
        """
        query = (query or "").strip()
        if not query:
            return SearchResult(status="EMPTY", query=query, debug={"reason": "blank_query"})

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return await self._search_once(query, top_k, use_rerank=use_rerank)
            except Exception as exc:  # 外部 DB/embedding 边界，统一转成显式 ERROR。
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0.05)
        return SearchResult(
            status="ERROR",
            query=query,
            debug={"error_type": type(last_error).__name__ if last_error else "unknown"},
            error="检索服务不可用：embedding 或数据库连续失败",
        )

    async def _search_once(self, query: str, top_k: int, *, use_rerank: bool = True) -> SearchResult:
        """单次混合检索。

        输入：已验证的查询与 Top-K。
        输出：融合后的结果、Parent 上下文或 EMPTY/LOW_SCORE。
        作用：把两路候选统一成可解释的 RRF 排名，再做 rerank 精排。
        实现：BM25/向量各取 candidate_k；RRF；reranker 失败按配置降级为 RRF。
        """
        index = await self._get_index()
        embedding, bm25_hits = await asyncio.gather(
            self.embedder.aembed_query(query),
            asyncio.to_thread(index.search, query, self.candidate_k),
        )
        if len(embedding) != self.settings.embedding_dim:
            raise RuntimeError("embedding 维度与配置不一致")
        vector_candidates = await self.repository.vector_search(
            embedding, limit=self.candidate_k, min_score=0.0
        )
        vector_pairs = [
            (material, similarity)
            for material, similarity in vector_candidates
            if similarity >= self.min_vector_score
        ]
        low_vector_count = len(vector_candidates) - len(vector_pairs)

        fused: dict[str, dict[str, Any]] = {}
        for hit in bm25_hits:
            source_id = hit.material.id
            item = fused.setdefault(source_id, _fused_item(hit.material))
            item["score"] += 1.0 / (self.rrf_k + hit.rank)
            item["bm25_rank"] = hit.rank
            item["matched_terms"].update(hit.matched_terms)
        for rank, (material, similarity) in enumerate(vector_pairs, start=1):
            item = fused.setdefault(material.id, _fused_item(material))
            item["score"] += 1.0 / (self.rrf_k + rank)
            item["vector_rank"] = rank
            item["vector_score"] = similarity

        results: list[SearchHit] = []
        for source_id, item in fused.items():
            material: Material = item["material"]
            results.append(
                SearchHit(
                    source_id=source_id,
                    text=material.text,
                    score=round(float(item["score"]), 6),
                    source=material.source,
                    source_type=material.source_type,
                    matched_terms=sorted(item["matched_terms"]),
                    bm25_rank=item["bm25_rank"],
                    vector_rank=item["vector_rank"],
                    parent_id=material.parent_id,
                    parent_text=material.parent_text,
                    heading_path=material.heading_path,
                )
            )
        results.sort(key=lambda hit: hit.score, reverse=True)
        results = results[: self.candidate_k]
        debug = {
            "bm25_count": len(bm25_hits),
            "vector_count": len(vector_pairs),
            "vector_candidates": len(vector_candidates),
            "low_vector_count": low_vector_count,
            "fused_count": len(fused),
            "candidate_count": len(results),
            "rerank_status": "disabled",
            "rrf_k": self.rrf_k,
            "min_vector_score": self.min_vector_score,
        }

        degraded = False
        risk: dict[str, Any] | None = None
        if not use_rerank:
            debug["rerank_status"] = "skipped"
        if use_rerank and self.reranker is not None and self.settings.rerank_enabled and len(results) > 1:
            try:
                ranked = await self._rerank_with_retry(query, [hit.text for hit in results])
                ordered: list[SearchHit] = []
                seen: set[str] = set()
                for index_value, score in ranked:
                    if index_value < 0 or index_value >= len(results):
                        continue
                    hit = results[index_value]
                    ordered.append(hit.model_copy(update={"rerank_score": score}))
                    seen.add(hit.source_id)
                for hit in results:
                    if hit.source_id not in seen:
                        ordered.append(hit)
                results = ordered
                debug["rerank_status"] = "ok"
                debug["rerank_returned"] = len(ranked)
            except Exception as exc:
                if not self.settings.rerank_fallback:
                    raise
                degraded = True
                risk = {
                    "code": "RERANK_DEGRADED",
                    "severity": "warn",
                    "message": "reranker 不可用，本次使用 RRF 排序",
                    "error_type": type(exc).__name__,
                }
                debug["rerank_status"] = "degraded"
                debug["rerank_error"] = type(exc).__name__

        results = results[:top_k]
        contexts = _build_contexts(results)
        if results:
            return SearchResult(
                status="OK",
                query=query,
                results=results,
                contexts=contexts,
                debug=debug,
                degraded=degraded,
                risk=risk,
            )
        if low_vector_count > 0:
            return SearchResult(
                status="LOW_SCORE", query=query, contexts=[], debug=debug, degraded=degraded, risk=risk
            )
        return SearchResult(
            status="EMPTY", query=query, contexts=[], debug=debug, degraded=degraded, risk=risk
        )

    async def _rerank_with_retry(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        """输入：query 与候选文本；输出：rerank 结果；作用：失败重试一次。"""
        assert self.reranker is not None
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return await self.reranker.rerank(
                    query, documents, top_n=len(documents)
                )
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0.05)
        raise last_error or RuntimeError("rerank failed")


def _fused_item(material: Material) -> dict[str, Any]:
    """输入：素材；输出：RRF 累加容器；作用：两路候选合并。"""
    return {
        "material": material,
        "score": 0.0,
        "matched_terms": set(),
        "bm25_rank": None,
        "vector_rank": None,
        "vector_score": None,
    }


def _build_contexts(results: list[SearchHit]) -> list[ContextBlock]:
    """输入：最终 child 命中；输出：去重后的 Parent 上下文；作用：短块召回、长块返回。"""
    contexts: list[ContextBlock] = []
    seen: set[str] = set()
    for hit in results:
        if not hit.parent_id or not hit.parent_text:
            continue
        if hit.parent_id in seen:
            for context in contexts:
                if context.parent_id == hit.parent_id and hit.source_id not in context.source_ids:
                    context.source_ids.append(hit.source_id)
            continue
        seen.add(hit.parent_id)
        contexts.append(
            ContextBlock(
                parent_id=hit.parent_id,
                text=hit.parent_text,
                heading_path=hit.heading_path,
                source=hit.source,
                source_ids=[hit.source_id],
            )
        )
    return contexts[:4]


class StaticRetrievalTool:
    """按队列返回固定检索结果的测试替身。"""

    def __init__(self, results: list[SearchResult | Exception] | None = None) -> None:
        """输入：预置结果队列；输出：替身；作用：固定验收场景。"""
        self._queue = list(results or [])

    async def search_materials(self, query: str, top_k: int = 5) -> SearchResult:
        """输入：查询；输出：下一条预置结果；作用：让图测试不访问外部服务。"""
        if not self._queue:
            return SearchResult(status="EMPTY", query=query, debug={"reason": "queue_exhausted"})
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def row_to_material(row: dict[str, Any]) -> Material:
    """输入：数据库行；输出：`Material`；作用：统一 DB 与领域模型转换。"""
    return Material(
        id=row["id"],
        text=row["text"],
        source=row["source"],
        source_type=row.get("source_type", "product_brief"),
        scope=row.get("scope"),
        tags=list(row.get("tags") or []),
        document_id=row.get("document_id"),
        chunk_id=row.get("chunk_id"),
        parent_id=row.get("parent_id"),
        parent_text=row.get("parent_text"),
        heading_path=list(row.get("heading_path") or []),
    )
