"""DashScope embedding 适配。

输入：查询文本。
输出：固定 1024 维向量。
作用：为 pgvector 检索提供 OpenAI 兼容的 embedding 客户端。
实现：复用 `langchain_openai.OpenAIEmbeddings`，启动时探测一次维度。
"""

from __future__ import annotations

from typing import Protocol

import httpx

from src.config import Settings


class EmbeddingClient(Protocol):
    """Embedding 最小接口，便于测试注入。"""

    async def aembed_query(self, text: str) -> list[float]:
        """输入：文本；输出：向量；作用：检索前编码查询。"""
        ...


class DashScopeEmbedder:
    """DashScope 原生 embedding 封装。

    当前工作区 MaaS 端点只暴露 DashScope 原生路径：
    `/api/v1/services/embeddings/text-embedding/text-embedding`，
    不提供 OpenAI 兼容的 `/embeddings`，因此不能复用 OpenAIEmbeddings。
    """

    def __init__(self, settings: Settings) -> None:
        """输入：配置；输出：客户端实例；作用：固定模型与维度；实现：记录原生端点。"""
        self.settings = settings
        self._endpoint = (
            settings.dashscope_base_url.rstrip("/")
            + "/services/embeddings/text-embedding/text-embedding"
        )

    def _payload(self, texts: list[str]) -> dict:
        """输入：文本列表；输出：DashScope 原生请求体；作用：统一单条/批量格式。"""
        return {
            "model": self.settings.dashscope_embedding_model,
            "input": {"texts": texts},
            "parameters": {"dimension": self.settings.embedding_dim},
        }

    def _headers(self) -> dict:
        """输入：无；输出：鉴权请求头；作用：避免在日志中打印 Key。"""
        return {
            "Authorization": f"Bearer {self.settings.dashscope_api_key}",
            "Content-Type": "application/json",
        }

    def _parse(self, response: httpx.Response) -> list[list[float]]:
        """输入：HTTP 响应；输出：按 text_index 排序的向量；作用：校验原生响应。"""
        if response.status_code >= 400:
            raise RuntimeError(f"DashScope embedding HTTP {response.status_code}")
        try:
            embeddings = response.json()["output"]["embeddings"]
            embeddings = sorted(embeddings, key=lambda item: item.get("text_index", 0))
            return [[float(value) for value in item["embedding"]] for item in embeddings]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("DashScope embedding 返回结构不合法") from exc

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """输入：文本列表；输出：向量列表；作用：种子数据同步写入。

        实现：按批调用，避免单次请求超过 DashScope 的批量上限。
        """
        batch_size = 10
        all_vectors: list[list[float]] = []
        with httpx.Client(timeout=self.settings.tool_timeout_seconds) as client:
            for start in range(0, len(texts), batch_size):
                batch = texts[start : start + batch_size]
                response = client.post(
                    self._endpoint,
                    headers=self._headers(),
                    json=self._payload(batch),
                )
                vectors = self._parse(response)
                if len(vectors) != len(batch):
                    raise RuntimeError("DashScope embedding 返回数量不匹配")
                all_vectors.extend(vectors)
        return all_vectors

    async def aembed_query(self, text: str) -> list[float]:
        """输入：查询文本；输出：向量列表；作用：供混合检索使用。"""
        async with httpx.AsyncClient(timeout=self.settings.tool_timeout_seconds) as client:
            response = await client.post(
                self._endpoint,
                headers=self._headers(),
                json=self._payload([text]),
            )
        vectors = self._parse(response)
        if not vectors:
            raise RuntimeError("DashScope embedding 返回空结果")
        return vectors[0]


class StaticEmbedder:
    """本地确定性 embedding，占位/测试使用。"""

    def __init__(self, dimension: int = 1024) -> None:
        """输入：维度；输出：实例；作用：无外部依赖地跑通检索流程。"""
        self.dimension = dimension

    async def aembed_query(self, text: str) -> list[float]:
        """输入：文本；输出：按字符权重生成的定长向量；作用：测试相似度路径。

        实现：把字符编码散列到固定桶，再做 L2 归一化；不用于生产语义检索。
        """
        vector = [0.0] * self.dimension
        for index, char in enumerate(text):
            vector[(ord(char) + index * 17) % self.dimension] += 1.0
        norm = sum(value * value for value in vector) ** 0.5 or 1.0
        return [value / norm for value in vector]


async def verify_embedding_dimension(embedder: EmbeddingClient, expected: int = 1024) -> None:
    """探测 embedding 维度。

    输入：embedding 客户端与期望维度。
    输出：无；维度不符时抛 `RuntimeError`。
    作用：阻止 `/readyz` 在错误配置下误报就绪。
    实现：真实调用 `embed_query("ping")`，检查返回长度。
    """
    vector = await embedder.aembed_query("ping")
    if len(vector) != expected:
        raise RuntimeError(f"embedding 维度不匹配：期望 {expected}，实际 {len(vector)}")
