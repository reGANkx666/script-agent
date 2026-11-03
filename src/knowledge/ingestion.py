"""知识库后台入库。

输入：已保存的文档元数据与原始文本。
输出：解析、Parent/Child 切片、embedding、原子发布。
作用：上传接口只负责创建任务；重活交给进程内后台任务并持续更新进度。
实现：asyncio.create_task + 线程执行同步 parser/embedder；发布由仓库事务保证。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from src.config import Settings
from src.knowledge.chunker import CHUNKER_VERSION, build_chunks
from src.knowledge.models import DocumentRecord, DocumentStatus, IngestionJobRecord
from src.knowledge.parsers import DocumentParseError, parse_document
from src.knowledge.repository import KnowledgeRepository
from src.tools.embeddings import EmbeddingClient

logger = logging.getLogger(__name__)


class KnowledgeIngestionService:
    """执行单次文档入库。"""

    def __init__(
        self,
        settings: Settings,
        repository: KnowledgeRepository,
        embedder: EmbeddingClient,
        *,
        on_published: Callable[[], Awaitable[None] | None] | None = None,
    ) -> None:
        """输入：配置、仓库、embedding、发布回调；输出：服务。"""
        self.settings = settings
        self.repository = repository
        self.embedder = embedder
        self.on_published = on_published

    async def ingest(self, document: DocumentRecord, job: IngestionJobRecord, content: bytes) -> None:
        """执行完整入库流程。

        输入：文档、任务、原始 bytes。
        输出：无；成功 READY，失败 FAILED。
        作用：上传后自动 active，无人工审核。
        实现：每阶段更新 job；发布使用仓库事务；错误只记录类型和可读信息。
        """
        try:
            await self.repository.update_document(
                document.id,
                status=DocumentStatus.PARSING.value,
                chunker_version=CHUNKER_VERSION,
                embedding_model=self.settings.dashscope_embedding_model,
            )
            await self._stage(job.id, DocumentStatus.PARSING, 15)

            parsed = await asyncio.to_thread(
                parse_document,
                document.filename,
                content,
                max_pdf_pages=self.settings.max_pdf_pages,
            )
            await self.repository.update_document(document.id, title=parsed.title)
            await self.repository.update_document(document.id, status=DocumentStatus.CHUNKING.value)
            await self._stage(job.id, DocumentStatus.CHUNKING, 35)

            chunks = build_chunks(document.id, parsed)
            child_chunks = [chunk for chunk in chunks if chunk.chunk_type.value == "child"]
            if not child_chunks:
                raise DocumentParseError("切片后没有可引用的 child")

            await self.repository.update_document(document.id, status=DocumentStatus.EMBEDDING.value)
            await self._stage(job.id, DocumentStatus.EMBEDDING, 55)
            vectors = await asyncio.to_thread(
                self.embedder.embed_documents,
                [chunk.embedding_text or chunk.text for chunk in child_chunks],
            )
            if len(vectors) != len(child_chunks):
                raise RuntimeError("embedding 返回数量与 child 数量不一致")
            if any(len(vector) != self.settings.embedding_dim for vector in vectors):
                raise RuntimeError("embedding 维度与配置不一致")

            await self._stage(job.id, DocumentStatus.EMBEDDING, 85)
            await self.repository.publish(
                document.id,
                job.id,
                chunks,
                vectors,
                source_name=document.filename,
            )
            if self.on_published is not None:
                maybe = self.on_published()
                if hasattr(maybe, "__await__"):
                    await maybe  # type: ignore[misc]
        except Exception as exc:
            logger.warning("knowledge ingestion failed document=%s type=%s", document.id, type(exc).__name__)
            message = f"{type(exc).__name__}: {str(exc)[:300]}"
            await self.repository.update_document(
                document.id,
                status=DocumentStatus.FAILED.value,
                error=message,
                active=False,
            )
            await self.repository.update_job(
                job.id,
                status=DocumentStatus.FAILED.value,
                stage=DocumentStatus.FAILED.value,
                error=message,
            )

    async def _stage(self, job_id: str, status: DocumentStatus, progress: int) -> None:
        """输入：任务与阶段；输出：无；作用：让前端轮询看到进度。"""
        await self.repository.update_job(
            job_id,
            status=status.value,
            stage=status.value,
            progress=progress,
            error=None,
        )


class IngestionManager:
    """进程内入库任务管理。

    输入：上传创建的任务。
    输出：后台 task；不阻塞 HTTP 请求。
    作用：与 RunManager 一样保持单进程简单实现，避免本轮引入 Celery。
    """

    def __init__(self, service: KnowledgeIngestionService) -> None:
        """输入：入库服务；输出：管理器。"""
        self.service = service
        self._tasks: dict[str, asyncio.Task] = {}

    def start(self, document: DocumentRecord, job: IngestionJobRecord, content: bytes) -> None:
        """输入：文档/任务/bytes；输出：无；作用：启动后台入库。"""
        task = asyncio.create_task(
            self.service.ingest(document, job, content),
            name=f"knowledge-ingestion-{document.id}",
        )
        self._tasks[document.id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(document.id, None))

    async def close(self) -> None:
        """输入：无；输出：无；作用：关闭时取消未完成任务。"""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
