"""RAG 检索评估。

输入：固定 query 集与期望 source_id。
输出：RRF 与 rerank 的 Recall@K、MRR、Precision@K 对照。
作用：把“rerank 有没有用”变成可重复的页面证据，而不是主观判断。
实现：同一检索服务分别关闭/开启 rerank；指标只基于返回的 source_id。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.tools.retrieval import HybridRetrievalService


@dataclass
class EvaluationCase:
    """单条评估 query。"""

    id: str
    query: str
    expected: list[str]
    note: str = ""


def load_evaluation_cases(path: Path | None = None) -> list[EvaluationCase]:
    """输入：可选 JSON 路径；输出：评估用例；作用：面板固定数据集。"""
    source = path or (Path(__file__).resolve().parents[2] / "data" / "knowledge_eval.json")
    payload = json.loads(source.read_text(encoding="utf-8"))
    return [
        EvaluationCase(
            id=str(item["id"]),
            query=str(item["query"]),
            expected=[str(value) for value in item.get("expected", [])],
            note=str(item.get("note", "")),
        )
        for item in payload
    ]


async def evaluate_retrieval(
    retrieval: HybridRetrievalService,
    *,
    k: int = 5,
    cases: list[EvaluationCase] | None = None,
) -> dict[str, Any]:
    """运行 RRF-only / rerank 两组检索并计算指标。

    输入：检索服务、K、可选用例。
    输出：可直接给 API/前端的结构。
    作用：展示 short-block 检索质量与 rerank 增益。
    实现：逐 query 调用两次；同一 dataset 同一 K，避免不可比。
    """
    selected = cases or load_evaluation_cases()
    rows: list[dict[str, Any]] = []
    rrf_values: list[dict[str, float]] = []
    rerank_values: list[dict[str, float]] = []
    wins = losses = ties = errors = 0

    for case in selected:
        rrf = await retrieval.search_materials(case.query, top_k=k, use_rerank=False)
        reranked = await retrieval.search_materials(case.query, top_k=k, use_rerank=True)
        if rrf.status == "ERROR" or reranked.status == "ERROR":
            errors += 1
        rrf_metrics = _metrics([hit.source_id for hit in rrf.results], case.expected, k)
        rerank_metrics = _metrics([hit.source_id for hit in reranked.results], case.expected, k)
        rrf_values.append(rrf_metrics)
        rerank_values.append(rerank_metrics)

        if rerank_metrics["mrr"] > rrf_metrics["mrr"]:
            wins += 1
        elif rerank_metrics["mrr"] < rrf_metrics["mrr"]:
            losses += 1
        else:
            ties += 1

        rows.append(
            {
                "id": case.id,
                "query": case.query,
                "note": case.note,
                "expected": case.expected,
                "rrf": {**rrf_metrics, "retrieved": [hit.source_id for hit in rrf.results]},
                "rerank": {
                    **rerank_metrics,
                    "retrieved": [hit.source_id for hit in reranked.results],
                    "degraded": reranked.degraded,
                },
            }
        )

    return {
        "k": k,
        "case_count": len(selected),
        "errors": errors,
        "rrf": _average(rrf_values),
        "rerank": _average(rerank_values),
        "comparison": {
            "rerank_wins": wins,
            "rerank_losses": losses,
            "ties": ties,
        },
        "cases": rows,
        "dataset": "data/knowledge_eval.json",
    }


def _metrics(retrieved: list[str], expected: list[str], k: int) -> dict[str, float]:
    """输入：返回 IDs 与期望 IDs；输出：Recall@K / Precision@K / MRR。"""
    expected_set = set(expected)
    hits = [source_id for source_id in retrieved[:k] if source_id in expected_set]
    recall = len(set(hits)) / len(expected_set) if expected_set else 0.0
    precision = len(hits) / len(retrieved[:k]) if retrieved[:k] else 0.0
    mrr = 0.0
    for index, source_id in enumerate(retrieved[:k], start=1):
        if source_id in expected_set:
            mrr = 1.0 / index
            break
    return {
        "recall": round(recall, 4),
        "precision": round(precision, 4),
        "mrr": round(mrr, 4),
    }


def _average(values: list[dict[str, float]]) -> dict[str, float]:
    """输入：指标列表；输出：平均值；作用：面板汇总。"""
    if not values:
        return {"recall": 0.0, "precision": 0.0, "mrr": 0.0}
    keys = values[0].keys()
    return {
        key: round(sum(item[key] for item in values) / len(values), 4)
        for key in keys
    }
