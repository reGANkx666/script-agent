"""中文 BM25 索引。

输入：active 素材和查询。
输出：词面 Top-N 结果及命中词。
作用：补足向量检索对产品型号、专有名词的精确召回。
实现：jieba 搜索模式分词，rank_bm25.BM25Okapi 打分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import jieba
from rank_bm25 import BM25Okapi

from src.domain.models import Material

_PUNCT_ONLY_RE = re.compile(r"^[\W_]+$", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """输入：文本；输出：token 列表；作用：BM25 查询与文档共用分词；实现：小写 + jieba 搜索模式。"""
    tokens = [token.strip().lower() for token in jieba.lcut_for_search((text or "").lower())]
    return [token for token in tokens if token and not _PUNCT_ONLY_RE.match(token)]


@dataclass
class BM25Hit:
    """BM25 单条命中。"""

    material: Material
    score: float
    rank: int
    matched_terms: list[str]


class BM25Index:
    """内存 BM25 索引。"""

    def __init__(self, materials: list[Material], k1: float = 1.5, b: float = 0.75) -> None:
        """输入：素材列表；输出：可检索索引；作用：启动/刷新时构建。"""
        self.materials = materials
        self._tokens = [tokenize(material.text) for material in materials]
        self._bm25 = BM25Okapi(self._tokens, k1=k1, b=b) if materials else None

    def search(self, query: str, top_k: int = 20) -> list[BM25Hit]:
        """执行 BM25 检索。

        输入：查询与 Top-K；输出：按分数降序的命中；作用：混合检索的词面一路。
        实现：过滤零分结果（无词面交集），并记录查询词命中。
        """
        if not self._bm25 or not self.materials:
            return []
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        scores = self._bm25.get_scores(query_tokens)
        ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)
        # BM25Okapi 在极小语料上可能给出非正 IDF；此时用词面交集做确定性兜底，
        # 保证单条种子素材仍能被精确词召回，同时不改变正常语料的 BM25 排序。
        use_overlap_fallback = not any(score > 0 for score in scores)
        hits: list[BM25Hit] = []
        for index, score in ranked:
            if len(hits) >= top_k:
                break
            matched = sorted(set(query_tokens).intersection(self._tokens[index]))
            if use_overlap_fallback:
                if not matched:
                    continue
                lexical_score = float(len(matched))
            else:
                if score <= 0:
                    continue
                lexical_score = float(score)
            hits.append(
                BM25Hit(
                    material=self.materials[index],
                    score=lexical_score,
                    rank=len(hits) + 1,
                    matched_terms=matched,
                )
            )
        return hits
