"""知识库 PostgreSQL 仓库。

输入：文档元数据、ChunkDraft、embedding 和材料投影。
输出：文档/任务/块记录与检索所需的 parent 上下文。
作用：把上传、发布、下架和预览统一到一个数据访问层。
实现：psycopg 原生 SQL；发布使用显式事务，避免半成品文档 active。
"""

from __future__ import annotations

from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from src.knowledge.models import ChunkDraft, ChunkRecord, DocumentRecord, IngestionJobRecord


def _vector_literal(vector: list[float]) -> str:
    """输入：向量；输出：pgvector 字面量；作用：参数化 `::vector`。"""
    return "[" + ",".join(f"{value:.8f}" for value in vector) + "]"


class KnowledgeRepository(Protocol):
    """入库 API 依赖的最小仓库接口。"""

    async def find_by_hash(self, content_hash: str) -> DocumentRecord | None: ...
    async def create_document(self, document: DocumentRecord) -> None: ...
    async def create_job(self, job: IngestionJobRecord) -> None: ...
    async def update_document(self, document_id: str, **fields: Any) -> None: ...
    async def update_job(self, job_id: str, **fields: Any) -> None: ...
    async def get_job(self, job_id: str) -> IngestionJobRecord | None: ...
    async def get_document(self, document_id: str) -> DocumentRecord | None: ...
    async def list_documents(self, limit: int = 50) -> list[DocumentRecord]: ...
    async def list_chunks(
        self, document_id: str, chunk_type: str | None = None
    ) -> list[ChunkRecord]: ...
    async def publish(
        self,
        document_id: str,
        job_id: str,
        chunks: list[ChunkDraft],
        embeddings: list[list[float]],
        *,
        source_name: str = "",
    ) -> None: ...
    async def deactivate(self, document_id: str) -> None: ...
    async def delete_document(self, document_id: str) -> None: ...


class PostgresKnowledgeRepository:
    """PostgreSQL 知识库实现。"""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """输入：连接池；输出：仓库；作用：与运行存储共享连接。"""
        self.pool = pool

    async def find_by_hash(self, content_hash: str) -> DocumentRecord | None:
        """输入：SHA-256；输出：已有文档；作用：上传幂等。"""
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, filename, mime, size_bytes, content_hash, status, error,
                           chunker_version, embedding_model, title, storage_path, active,
                           created_at, updated_at
                    FROM knowledge_documents WHERE content_hash = %s
                    """,
                    (content_hash,),
                )
                row = await cur.fetchone()
        return _document_from_row(row) if row else None

    async def create_document(self, document: DocumentRecord) -> None:
        """输入：文档元数据；输出：无；作用：上传后立即建记录。"""
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO knowledge_documents
                      (id, filename, mime, size_bytes, content_hash, status, error,
                       chunker_version, embedding_model, title, storage_path, active)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,false)
                    """,
                    (
                        document.id,
                        document.filename,
                        document.mime,
                        document.size_bytes,
                        document.content_hash,
                        document.status,
                        document.error,
                        document.chunker_version,
                        document.embedding_model,
                        document.title,
                        document.storage_path,
                    ),
                )

    async def create_job(self, job: IngestionJobRecord) -> None:
        """输入：任务状态；输出：无；作用：让 API 可轮询进度。"""
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO knowledge_ingestion_jobs
                      (id, document_id, status, stage, progress, error)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (job.id, job.document_id, job.status, job.stage, job.progress, job.error),
                )

    async def update_document(self, document_id: str, **fields: Any) -> None:
        """输入：文档 id 与字段；输出：无；作用：状态/错误/标题更新。"""
        allowed = {"status", "error", "title", "active", "embedding_model", "chunker_version"}
        fields = {key: value for key, value in fields.items() if key in allowed}
        if not fields:
            return
        assignments = ", ".join(f"{key} = %s" for key in fields)
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"UPDATE knowledge_documents SET {assignments}, updated_at = now() WHERE id = %s",
                    (*fields.values(), document_id),
                )

    async def update_job(self, job_id: str, **fields: Any) -> None:
        """输入：任务 id 与字段；输出：无；作用：推进解析/切片/embedding 进度。"""
        allowed = {"status", "stage", "progress", "error"}
        fields = {key: value for key, value in fields.items() if key in allowed}
        if not fields:
            return
        assignments = ", ".join(f"{key} = %s" for key in fields)
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"UPDATE knowledge_ingestion_jobs SET {assignments}, updated_at = now() WHERE id = %s",
                    (*fields.values(), job_id),
                )

    async def get_job(self, job_id: str) -> IngestionJobRecord | None:
        """输入：任务 id；输出：任务；作用：轮询接口。"""
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, document_id, status, stage, progress, error, created_at, updated_at
                    FROM knowledge_ingestion_jobs WHERE id = %s
                    """,
                    (job_id,),
                )
                row = await cur.fetchone()
        return _job_from_row(row) if row else None

    async def get_document(self, document_id: str) -> DocumentRecord | None:
        """输入：文档 id；输出：文档及块统计；作用：详情和前端。"""
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT d.id, d.filename, d.mime, d.size_bytes, d.content_hash, d.status, d.error,
                           d.chunker_version, d.embedding_model, d.title, d.storage_path, d.active,
                           d.created_at, d.updated_at,
                           COUNT(c.id) FILTER (WHERE c.chunk_type = 'parent') AS parent_count,
                           COUNT(c.id) FILTER (WHERE c.chunk_type = 'child') AS child_count
                    FROM knowledge_documents d
                    LEFT JOIN knowledge_chunks c ON c.document_id = d.id
                    WHERE d.id = %s
                    GROUP BY d.id
                    """,
                    (document_id,),
                )
                row = await cur.fetchone()
        return _document_from_row(row) if row else None

    async def list_documents(self, limit: int = 50) -> list[DocumentRecord]:
        """输入：上限；输出：最近文档；作用：知识库列表。"""
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT d.id, d.filename, d.mime, d.size_bytes, d.content_hash, d.status, d.error,
                           d.chunker_version, d.embedding_model, d.title, d.storage_path, d.active,
                           d.created_at, d.updated_at,
                           COUNT(c.id) FILTER (WHERE c.chunk_type = 'parent') AS parent_count,
                           COUNT(c.id) FILTER (WHERE c.chunk_type = 'child') AS child_count
                    FROM knowledge_documents d
                    LEFT JOIN knowledge_chunks c ON c.document_id = d.id
                    GROUP BY d.id
                    ORDER BY d.created_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = await cur.fetchall()
        return [_document_from_row(row) for row in rows]

    async def list_chunks(
        self, document_id: str, chunk_type: str | None = None
    ) -> list[ChunkRecord]:
        """输入：文档 id 与类型；输出：块列表；作用：Parent/Child 预览。"""
        sql = """
            SELECT c.id, c.document_id, c.parent_id, c.chunk_type, c.ordinal,
                   c.heading_path, c.text, c.token_count, c.active, m.id AS material_id
            FROM knowledge_chunks c
            LEFT JOIN materials m ON m.chunk_id = c.id
            WHERE c.document_id = %s
        """
        params: list[Any] = [document_id]
        if chunk_type:
            sql += " AND c.chunk_type = %s"
            params.append(chunk_type)
        sql += " ORDER BY c.ordinal"
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        return [
            ChunkRecord(
                id=row[0],
                document_id=str(row[1]),
                parent_id=row[2],
                chunk_type=row[3],
                ordinal=row[4],
                heading_path=list(row[5] or []),
                text=row[6],
                token_count=row[7],
                active=row[8],
                material_id=row[9],
            )
            for row in rows
        ]

    async def publish(
        self,
        document_id: str,
        job_id: str,
        chunks: list[ChunkDraft],
        embeddings: list[list[float]],
        *,
        source_name: str = "",
    ) -> None:
        """输入：文档、任务、块和 child 向量；输出：无；作用：原子发布。

        实现：同一事务写入 parent/child chunks 与 materials；全部成功后 active。
        """
        embedding_by_id = {
            chunk.id: vector
            for chunk, vector in zip(
                [item for item in chunks if item.chunk_type.value == "child"], embeddings, strict=True
            )
        }
        async with self.pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM materials WHERE document_id = %s", (document_id,))
                    await cur.execute("DELETE FROM knowledge_chunks WHERE document_id = %s", (document_id,))
                    for chunk in chunks:
                        vector = embedding_by_id.get(chunk.id)
                        await cur.execute(
                            """
                            INSERT INTO knowledge_chunks
                              (id, document_id, parent_id, chunk_type, ordinal, heading_path,
                               text, token_count, embedding, metadata, active)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true)
                            """,
                            (
                                chunk.id,
                                chunk.document_id,
                                chunk.parent_id,
                                chunk.chunk_type.value,
                                chunk.ordinal,
                                chunk.heading_path,
                                chunk.text,
                                chunk.token_count,
                                _vector_literal(vector) if vector else None,
                                Jsonb(chunk.metadata),
                            ),
                        )
                        if chunk.chunk_type.value == "child" and chunk.material_id:
                            await cur.execute(
                                """
                                INSERT INTO materials
                                  (id, text, source, source_type, scope, tags, active,
                                   embedding, document_id, chunk_id, parent_id, updated_at)
                                VALUES (%s,%s,%s,'uploaded_document',%s,%s,true,%s,%s,%s,%s,now())
                                ON CONFLICT (id) DO UPDATE SET
                                  text=EXCLUDED.text, source=EXCLUDED.source,
                                  embedding=EXCLUDED.embedding, document_id=EXCLUDED.document_id,
                                  chunk_id=EXCLUDED.chunk_id, parent_id=EXCLUDED.parent_id,
                                  active=true, updated_at=now()
                                """,
                                (
                                    chunk.material_id,
                                    chunk.text,
                                    source_name or document_id,
                                    chunk.metadata.get("scope"),
                                    ["uploaded", "child"],
                                    _vector_literal(vector) if vector else None,
                                    document_id,
                                    chunk.id,
                                    chunk.parent_id,
                                ),
                            )
                    await cur.execute(
                        """
                        UPDATE knowledge_documents
                        SET status='READY', error=NULL, active=true, updated_at=now()
                        WHERE id=%s
                        """,
                        (document_id,),
                    )
                    await cur.execute(
                        """
                        UPDATE knowledge_ingestion_jobs
                        SET status='READY', stage='READY', progress=100, error=NULL, updated_at=now()
                        WHERE id=%s
                        """,
                        (job_id,),
                    )

    async def deactivate(self, document_id: str) -> None:
        """输入：文档 id；输出：无；作用：文章与材料同时退出检索。"""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE knowledge_documents SET active=false, updated_at=now() WHERE id=%s",
                        (document_id,),
                    )
                    await cur.execute(
                        "UPDATE knowledge_chunks SET active=false WHERE document_id=%s",
                        (document_id,),
                    )
                    await cur.execute(
                        "UPDATE materials SET active=false, updated_at=now() WHERE document_id=%s",
                        (document_id,),
                    )

    async def delete_document(self, document_id: str) -> None:
        """输入：文档 id；输出：无；作用：彻底删除文档与派生数据。"""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM materials WHERE document_id=%s", (document_id,))
                    await cur.execute("DELETE FROM knowledge_chunks WHERE document_id=%s", (document_id,))
                    await cur.execute(
                        "DELETE FROM knowledge_ingestion_jobs WHERE document_id=%s", (document_id,)
                    )
                    await cur.execute("DELETE FROM knowledge_documents WHERE id=%s", (document_id,))


def _document_from_row(row: tuple[Any, ...]) -> DocumentRecord:
    """输入：SQL 行；输出：DocumentRecord；作用：API/服务共用。"""
    return DocumentRecord(
        id=str(row[0]),
        filename=row[1],
        mime=row[2] or "",
        size_bytes=int(row[3] or 0),
        content_hash=row[4],
        status=row[5],
        error=row[6],
        chunker_version=row[7],
        embedding_model=row[8] or "",
        title=row[9],
        storage_path=row[10],
        active=bool(row[11]),
        created_at=row[12],
        updated_at=row[13],
        parent_count=int(row[14] or 0) if len(row) > 14 else 0,
        child_count=int(row[15] or 0) if len(row) > 15 else 0,
    )


def _job_from_row(row: tuple[Any, ...]) -> IngestionJobRecord:
    """输入：SQL 行；输出：IngestionJobRecord。"""
    return IngestionJobRecord(
        id=str(row[0]),
        document_id=str(row[1]),
        status=row[2],
        stage=row[3],
        progress=int(row[4] or 0),
        error=row[5],
        created_at=row[6],
        updated_at=row[7],
    )
