"""PostgreSQL 素材与规则仓库。

输入：查询文本向量、平台。
输出：`Material` 或规则快照。
作用：给混合检索和规则节点提供数据库实现。
实现：psycopg 原生 SQL + pgvector 余弦距离；不引入 ORM。
"""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from src.domain.models import Material
from src.tools.retrieval import row_to_material
from src.tools.rule_repository import RuleSnapshot


def _vector_literal(vector: list[float]) -> str:
    """输入：浮点向量；输出：pgvector 文本字面量；作用：参数化 `::vector`；实现：方括号拼接。"""
    return "[" + ",".join(f"{value:.8f}" for value in vector) + "]"


class PostgresMaterialRepository:
    """素材仓库。"""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """输入：连接池；输出：仓库；作用：BM25 与向量检索共用。"""
        self.pool = pool

    async def list_active(self) -> list[Material]:
        """输出：active 素材；作用：构建 BM25 索引；实现：单表 SELECT。"""
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT m.id, m.text, m.source, m.source_type, m.scope, m.tags,
                           m.document_id::text, m.chunk_id, m.parent_id,
                           c.text AS parent_text, c.heading_path
                    FROM materials m
                    LEFT JOIN knowledge_chunks c ON c.id = m.parent_id
                    WHERE m.active = true
                    ORDER BY m.id
                    """
                )
                rows = await cur.fetchall()
        return [
            row_to_material(
                {
                    "id": row[0],
                    "text": row[1],
                    "source": row[2],
                    "source_type": row[3],
                    "scope": row[4],
                    "tags": row[5],
                    "document_id": row[6],
                    "chunk_id": row[7],
                    "parent_id": row[8],
                    "parent_text": row[9],
                    "heading_path": row[10],
                }
            )
            for row in rows
        ]

    async def vector_search(
        self, embedding: list[float], limit: int = 20, min_score: float = 0.25
    ) -> list[tuple[Material, float]]:
        """输入：查询向量；输出：相似素材与分数；作用：向量一路召回。

        实现：`1 - cosine_distance` 计分；先按 HNSW 取 Top-N，再过滤低分。
        """
        literal = _vector_literal(embedding)
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT m.id, m.text, m.source, m.source_type, m.scope, m.tags,
                           m.document_id::text, m.chunk_id, m.parent_id,
                           c.text AS parent_text, c.heading_path, score
                    FROM (
                      SELECT m.*, 1 - (m.embedding <=> %s::vector) AS score
                      FROM materials m
                      WHERE m.active = true AND m.embedding IS NOT NULL
                      ORDER BY m.embedding <=> %s::vector
                      LIMIT %s
                    ) m
                    LEFT JOIN knowledge_chunks c ON c.id = m.parent_id
                    WHERE m.score >= %s
                    ORDER BY m.score DESC
                    """,
                    (literal, literal, limit, min_score),
                )
                rows = await cur.fetchall()
        return [
            (
                row_to_material(
                    {
                        "id": row[0],
                        "text": row[1],
                        "source": row[2],
                        "source_type": row[3],
                        "scope": row[4],
                        "tags": row[5],
                        "document_id": row[6],
                        "chunk_id": row[7],
                        "parent_id": row[8],
                        "parent_text": row[9],
                        "heading_path": row[10],
                    }
                ),
                float(row[11]),
            )
            for row in rows
        ]


class PostgresRuleProvider:
    """品牌/平台规则仓库。"""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """输入：连接池；输出：提供器；作用：规则节点按平台加载。"""
        self.pool = pool

    async def get_rules(self, platform: str | None) -> RuleSnapshot:
        """输入：平台；输出：规则快照；作用：供确定性规则检查。

        实现：品牌规则取全部 active；平台规则按平台过滤。
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, rule, severity FROM brand_rules WHERE active = true ORDER BY id"
                )
                brand_rows = await cur.fetchall()
                if platform:
                    await cur.execute(
                        """
                        SELECT code, rule, severity FROM platform_rules
                        WHERE platform = %s AND active = true
                        ORDER BY code
                        """,
                        (platform,),
                    )
                    platform_rows = await cur.fetchall()
                else:
                    platform_rows = []
        brand = [
            {"id": row[0], "severity": row[2], **dict(row[1] or {})}
            for row in brand_rows
        ]
        platform_items = [
            {"code": row[0], "severity": row[2], **dict(row[1] or {})}
            for row in platform_rows
        ]
        return RuleSnapshot(brand=brand, platform=platform_items)
