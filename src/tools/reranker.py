"""DashScope reranker 适配。

输入：查询与候选 child 文本。
输出：按相关性重排的候选下标与分数。
作用：在 BM25 + 向量 RRF 之后做精排，提升短块召回质量。
实现：httpx 调用 DashScope 原生 text-rerank 接口；失败由检索服务显式降级。
"""

from __future__ import annotations

from typing import Protocol

import httpx

from src.config import Settings


class Reranker(Protocol):
    """最小 rerank 接口。"""

    async def rerank(
        self, query: str, documents: list[str], top_n: int
    ) -> list[tuple[int, float]]:
        """输入：查询、候选文本、Top-N；输出：(原下标, 分数)。"""
        ...


class DashScopeReranker:
    """DashScope gte-rerank-v2 客户端。"""

    def __init__(self, settings: Settings) -> None:
        """输入：配置；输出：客户端；作用：固定原生 rerank 端点。"""
        self.settings = settings
        self._endpoint = (
            settings.dashscope_base_url.rstrip("/")
            + "/services/rerank/text-rerank/text-rerank"
        )

    async def rerank(
        self, query: str, documents: list[str], top_n: int
    ) -> list[tuple[int, float]]:
        """调用原生 rerank。

        输入：查询、候选文本、Top-N。
        输出：按分数降序的 (候选下标, 分数)。
        作用：只对 RRF 候选重排，不重新生成事实。
        """
        if not documents:
            return []
        payload = {
            "model": self.settings.rerank_model,
            "input": {"query": query, "documents": documents},
            "parameters": {"return_documents": False, "top_n": min(top_n, len(documents))},
        }
        headers = {
            "Authorization": f"Bearer {self.settings.dashscope_api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.settings.rerank_timeout_seconds) as client:
            response = await client.post(self._endpoint, headers=headers, json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"rerank HTTP {response.status_code}")
        try:
            results = response.json()["output"]["results"]
            parsed = [
                (int(item["index"]), float(item["relevance_score"]))
                for item in results
                if "index" in item
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("rerank 返回结构不合法") from exc
        parsed.sort(key=lambda item: item[1], reverse=True)
        return parsed[:top_n]
