"""RAG 评估指标测试。"""

from __future__ import annotations

from src.domain.models import SearchHit, SearchResult
from src.knowledge.evaluation import EvaluationCase, evaluate_retrieval


class FakeRetrieval:
    """按 query 返回固定 RRF / rerank 排序。"""

    async def search_materials(self, query: str, top_k: int = 5, use_rerank: bool = True):
        if query == "q1":
            ids = ["A", "B"] if not use_rerank else ["B", "A"]
        else:
            ids = ["X", "A"] if not use_rerank else ["A", "X"]
        return SearchResult(
            status="OK",
            query=query,
            results=[
                SearchHit(source_id=source_id, text=source_id, score=1.0 - index * 0.1)
                for index, source_id in enumerate(ids[:top_k])
            ],
            debug={"rerank_status": "ok" if use_rerank else "skipped"},
        )


async def test_evaluation_reports_recall_and_rerank_comparison() -> None:
    """输入：固定候选；输出：Recall/MRR 与 rerank 胜负统计。"""
    cases = [
        EvaluationCase(id="Q1", query="q1", expected=["A"]),
        EvaluationCase(id="Q2", query="q2", expected=["A"]),
    ]
    result = await evaluate_retrieval(FakeRetrieval(), k=2, cases=cases)  # type: ignore[arg-type]
    assert result["case_count"] == 2
    assert result["rrf"]["recall"] == 1.0
    assert result["rerank"]["recall"] == 1.0
    assert result["comparison"]["rerank_wins"] == 1
    assert result["comparison"]["rerank_losses"] == 1
    assert result["cases"][0]["rrf"]["retrieved"] == ["A", "B"]
    assert result["cases"][0]["rerank"]["retrieved"] == ["B", "A"]
