"""混合检索测试。"""

from __future__ import annotations

from src.domain.models import Material
from src.tools.retrieval import HybridRetrievalService
from tests.helpers import make_test_settings

MATERIALS = [
    Material(id="P01", text="产品支持字幕功能", source="产品说明 v1.2"),
    Material(id="P02", text="产品支持导出视频文件", source="产品说明 v1.2"),
    Material(id="P03", text="产品支持横屏模式", source="产品说明 v1.2"),
]


class FakeRepository:
    """可控素材仓库。"""

    def __init__(self) -> None:
        """输入：无；输出：仓库；作用：固定向量排序。"""
        self.vector_calls = 0

    async def list_active(self) -> list[Material]:
        """输出：素材；作用：BM25 索引。"""
        return list(MATERIALS)

    async def vector_search(self, embedding, limit=20, min_score=0.25):
        """输入：向量；输出：P02→P01；作用：验证 RRF 融合。"""
        self.vector_calls += 1
        return [(MATERIALS[1], 0.91), (MATERIALS[0], 0.82)]


class FakeEmbedder:
    """固定向量。"""

    async def aembed_query(self, text: str) -> list[float]:
        """输入：文本；输出：1024 维固定向量。"""
        return [0.1] * 1024


class FailingEmbedder:
    """始终失败的 embedding。"""

    def __init__(self) -> None:
        """输入：无；输出：替身；作用：统计重试次数。"""
        self.calls = 0

    async def aembed_query(self, text: str) -> list[float]:
        """输入：文本；输出：抛异常；作用：验证 ERROR 不降级。"""
        self.calls += 1
        raise TimeoutError("embedding timeout")


class WrongDimensionEmbedder(FakeEmbedder):
    """维度错误 embedding。"""

    async def aembed_query(self, text: str) -> list[float]:
        """输入：文本；输出：错误维度；作用：阻止不兼容索引。"""
        return [0.1] * 3


class LowScoreRepository(FakeRepository):
    """只返回低相关候选。"""

    async def vector_search(self, embedding, limit=20, min_score=0.25):
        """输入：向量；输出：低分候选；作用：验证 LOW_SCORE。"""
        self.vector_calls += 1
        return [(MATERIALS[0], 0.1)]


def test_bm25_single_document_uses_overlap_fallback() -> None:
    """输入：只有一条素材；输出：精确词仍可被 BM25 召回。"""
    from src.tools.bm25 import BM25Index

    index = BM25Index([MATERIALS[0]])
    hits = index.search("字幕")
    assert len(hits) == 1
    assert hits[0].material.id == "P01"
    assert "字幕" in hits[0].matched_terms


async def test_low_score_is_explicit() -> None:
    """输入：只有低于阈值的向量候选；输出：LOW_SCORE，不进入融合。"""
    service = HybridRetrievalService(
        LowScoreRepository(), FakeEmbedder(), make_test_settings()
    )
    result = await service.search_materials("天空", top_k=5)
    assert result.status == "LOW_SCORE"
    assert result.results == []
    assert result.debug["low_vector_count"] == 1


async def test_rrf_orders_lexical_and_semantic_results() -> None:
    """输入：词面 P01 第一、向量 P02 第一；输出：RRF 融合结果含两路排名。"""
    service = HybridRetrievalService(FakeRepository(), FakeEmbedder(), make_test_settings())
    result = await service.search_materials("字幕", top_k=5)
    assert result.status == "OK"
    assert result.results[0].source_id == "P01"
    assert result.results[0].bm25_rank == 1
    assert result.results[0].vector_rank == 2
    assert "字幕" in result.results[0].matched_terms
    assert result.debug["bm25_count"] == 1


async def test_embedding_failure_returns_error_after_retry() -> None:
    """输入：embedding 连续失败；输出：ERROR 且恰好重试一次，不静默走纯 BM25。"""
    embedder = FailingEmbedder()
    service = HybridRetrievalService(FakeRepository(), embedder, make_test_settings())
    result = await service.search_materials("字幕", top_k=5)
    assert result.status == "ERROR"
    assert result.error
    assert embedder.calls == 2


async def test_wrong_dimension_is_error() -> None:
    """输入：维度不是 1024；输出：ERROR。"""
    service = HybridRetrievalService(FakeRepository(), WrongDimensionEmbedder(), make_test_settings())
    result = await service.search_materials("字幕", top_k=5)
    assert result.status == "ERROR"


class FakeReranker:
    """固定把向量第一的 P02 放到第一位。"""

    async def rerank(self, query, documents, top_n):
        return [(1, 0.99), (0, 0.2)]


class FailingReranker:
    """始终失败，用于验证 RRF 降级。"""

    async def rerank(self, query, documents, top_n):
        raise TimeoutError("rerank timeout")


async def test_rerank_reorders_candidates() -> None:
    """输入：reranker 成功；输出：P02 被精排到第一，并保留 rerank_score。"""
    service = HybridRetrievalService(
        FakeRepository(), FakeEmbedder(), make_test_settings(), reranker=FakeReranker()
    )
    result = await service.search_materials("字幕", top_k=5)
    assert result.status == "OK"
    assert result.results[0].source_id == "P02"
    assert result.results[0].rerank_score == 0.99
    assert result.debug["rerank_status"] == "ok"


async def test_rerank_failure_degrades_explicitly() -> None:
    """输入：reranker 连续失败；输出：RRF 结果 + RERANK_DEGRADED，不整次失败。"""
    service = HybridRetrievalService(
        FakeRepository(), FakeEmbedder(), make_test_settings(), reranker=FailingReranker()
    )
    result = await service.search_materials("字幕", top_k=5)
    assert result.status == "OK"
    assert result.results
    assert result.degraded is True
    assert result.risk["code"] == "RERANK_DEGRADED"
    assert result.debug["rerank_status"] == "degraded"


async def test_parent_context_is_returned_once() -> None:
    """输入：同一 parent 下的两个 child；输出：短块命中但 parent 上下文只返回一次。"""
    material = Material(
        id="M-1",
        text="字幕应与口播同步出现",
        source="guide.md",
        parent_id="p-1",
        parent_text="字幕设置\n字幕应与口播同步出现。\n每行字幕只表达一个意思。",
        heading_path=["字幕设置"],
    )

    class ParentRepository(FakeRepository):
        async def list_active(self):
            return [material]

        async def vector_search(self, embedding, limit=20, min_score=0.25):
            return [(material, 0.9)]

    service = HybridRetrievalService(ParentRepository(), FakeEmbedder(), make_test_settings())
    result = await service.search_materials("字幕", top_k=5)
    assert result.status == "OK"
    assert len(result.contexts) == 1
    assert result.contexts[0].parent_id == "p-1"
    assert result.contexts[0].text.startswith("字幕设置")
