"""知识库上传、预览与检索调试 API。"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile

from src.api.deps import get_container
from src.api.schemas import (
    KnowledgeChunkResponse,
    KnowledgeDocumentResponse,
    KnowledgeJobResponse,
    KnowledgeSearchRequest,
    KnowledgeUploadResponse,
)
from src.config import Settings
from src.domain.models import SearchResult
from src.knowledge.evaluation import evaluate_retrieval
from src.knowledge.models import (
    DocumentRecord,
    DocumentStatus,
    IngestionJobRecord,
)
from src.knowledge.parsers import SUPPORTED_EXTENSIONS
from src.runtime.container import AppContainer

router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])


def _require_repository(container: AppContainer):
    """输入：容器；输出：知识库仓库；作用：不可用时明确 503。"""
    if not container.ready or container.knowledge_repository is None:
        raise HTTPException(
            status_code=503,
            detail={"message": "知识库未就绪", "errors": container.readiness.errors},
        )
    return container.knowledge_repository


def _document_response(document: DocumentRecord) -> KnowledgeDocumentResponse:
    """输入：文档记录；输出：API 摘要。"""
    return KnowledgeDocumentResponse(
        id=document.id,
        filename=document.filename,
        mime=document.mime,
        size_bytes=document.size_bytes,
        status=document.status,
        error=document.error,
        title=document.title,
        active=document.active,
        parent_count=document.parent_count,
        child_count=document.child_count,
        content_hash=document.content_hash,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _job_response(job: IngestionJobRecord) -> KnowledgeJobResponse:
    """输入：任务记录；输出：API 摘要。"""
    return KnowledgeJobResponse(**asdict(job))


@router.post("/documents", response_model=KnowledgeUploadResponse, status_code=202)
async def upload_document(
    file: UploadFile = File(...),
    container: AppContainer = Depends(get_container),
) -> KnowledgeUploadResponse:
    """上传文档并创建后台入库任务。

    输入：multipart 文件。
    输出：202 + 文档/任务；重复内容返回已有文档。
    作用：无人工审核；全部切片/embedding 成功后自动 active。
    实现：哈希去重 → 保存原文件 → 建记录/任务 → IngestionManager 后台执行。
    """
    repository = _require_repository(container)
    settings: Settings = container.settings
    if container.ingestion_manager is None:
        raise HTTPException(status_code=503, detail="入库管理器未初始化")

    filename = Path(file.filename or "upload").name
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=415, detail=f"不支持的文件类型：{suffix or '无扩展名'}")
    content = await file.read()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if not content:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"文件超过 {settings.max_upload_mb} MB 限制")

    content_hash = hashlib.sha256(content).hexdigest()
    existing = await repository.find_by_hash(content_hash)
    if existing is not None:
        if existing.status == DocumentStatus.READY.value:
            refreshed = await repository.get_document(existing.id)
            return KnowledgeUploadResponse(
                document=_document_response(refreshed or existing),
                deduplicated=True,
            )
        if existing.status not in {DocumentStatus.FAILED.value, DocumentStatus.INACTIVE.value}:
            raise HTTPException(status_code=409, detail="相同文件正在入库，请稍后查询")
        await repository.delete_document(existing.id)

    document_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    storage_dir = Path(settings.uploads_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = storage_dir / f"{document_id}{suffix}"
    await asyncio.to_thread(storage_path.write_bytes, content)

    document = DocumentRecord(
        id=document_id,
        filename=filename,
        mime=file.content_type or "",
        size_bytes=len(content),
        content_hash=content_hash,
        status=DocumentStatus.QUEUED.value,
        chunker_version="pc-v1",
        embedding_model=settings.dashscope_embedding_model,
        title=Path(filename).stem,
        storage_path=str(storage_path),
        active=False,
    )
    job = IngestionJobRecord(
        id=job_id,
        document_id=document_id,
        status=DocumentStatus.QUEUED.value,
        stage=DocumentStatus.QUEUED.value,
        progress=0,
    )
    await repository.create_document(document)
    await repository.create_job(job)
    container.ingestion_manager.start(document, job, content)
    return KnowledgeUploadResponse(
        document=_document_response(document),
        job=_job_response(job),
        deduplicated=False,
    )


@router.get("/documents", response_model=list[KnowledgeDocumentResponse])
async def list_documents(
    limit: int = Query(default=50, ge=1, le=200),
    container: AppContainer = Depends(get_container),
) -> list[KnowledgeDocumentResponse]:
    """输入：数量上限；输出：最近文档列表。"""
    repository = _require_repository(container)
    documents = await repository.list_documents(limit=limit)
    return [_document_response(document) for document in documents]


@router.get("/documents/{document_id}", response_model=KnowledgeDocumentResponse)
async def get_document(
    document_id: str,
    container: AppContainer = Depends(get_container),
) -> KnowledgeDocumentResponse:
    """输入：文档 id；输出：文档详情。"""
    repository = _require_repository(container)
    document = await repository.get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return _document_response(document)


@router.get("/documents/{document_id}/chunks", response_model=list[KnowledgeChunkResponse])
async def list_chunks(
    document_id: str,
    chunk_type: Literal["parent", "child"] | None = Query(default=None),
    container: AppContainer = Depends(get_container),
) -> list[KnowledgeChunkResponse]:
    """输入：文档 id 与块类型；输出：Parent/Child 预览。"""
    repository = _require_repository(container)
    document = await repository.get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    chunks = await repository.list_chunks(document_id, chunk_type=chunk_type)
    return [KnowledgeChunkResponse(**asdict(chunk)) for chunk in chunks]


@router.get("/jobs/{job_id}", response_model=KnowledgeJobResponse)
async def get_job(
    job_id: str,
    container: AppContainer = Depends(get_container),
) -> KnowledgeJobResponse:
    """输入：任务 id；输出：入库进度。"""
    repository = _require_repository(container)
    job = await repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _job_response(job)


@router.post("/documents/{document_id}/deactivate", response_model=KnowledgeDocumentResponse)
async def deactivate_document(
    document_id: str,
    container: AppContainer = Depends(get_container),
) -> KnowledgeDocumentResponse:
    """输入：文档 id；输出：下架后的文档；作用：立即退出检索。"""
    repository = _require_repository(container)
    document = await repository.get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    await repository.deactivate(document_id)
    if container.retrieval is not None:
        container.retrieval.invalidate_index()
    refreshed = await repository.get_document(document_id)
    return _document_response(refreshed or document)


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: str,
    container: AppContainer = Depends(get_container),
) -> Response:
    """永久删除文档、切片、材料、任务和原文件。

    输入：文档 id。
    输出：204。
    作用：只删除已结束入库的文档；正在入库的文档拒绝删除。
    实现：校验状态 → 事务删除 DB → 安全删除 uploads 原文件 → 失效 BM25。
    """
    repository = _require_repository(container)
    document = await repository.get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if document.status in {
        DocumentStatus.QUEUED.value,
        DocumentStatus.PARSING.value,
        DocumentStatus.CHUNKING.value,
        DocumentStatus.EMBEDDING.value,
    }:
        raise HTTPException(status_code=409, detail="文档正在入库，完成后再删除")
    await repository.delete_document(document_id)
    _remove_uploaded_file(container.settings.uploads_dir, document.storage_path)
    if container.retrieval is not None:
        container.retrieval.invalidate_index()
    return Response(status_code=204)


def _remove_uploaded_file(uploads_dir: str, storage_path: str | None) -> None:
    """输入：上传目录与存储路径；输出：无；作用：只删除受控 uploads 内的原文件。"""
    if not storage_path:
        return
    root = Path(uploads_dir).resolve()
    candidate = Path(storage_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return
    try:
        if candidate.is_file():
            candidate.unlink()
    except OSError:
        # DB 已删除；原文件清理失败只留下孤儿文件，不回滚业务删除。
        return


@router.get("/evaluation")
async def evaluation(
    k: int = Query(default=5, ge=1, le=10),
    container: AppContainer = Depends(get_container),
) -> dict:
    """输入：K；输出：RRF 与 rerank 的检索评估。"""
    if not container.ready or container.retrieval is None:
        raise HTTPException(status_code=503, detail="检索服务未就绪")
    return await evaluate_retrieval(container.retrieval, k=k)


@router.post("/search", response_model=SearchResult)
async def debug_search(
    payload: KnowledgeSearchRequest,
    container: AppContainer = Depends(get_container),
) -> SearchResult:
    """输入：query/top_k；输出：含 rerank 调试信息的检索结果。"""
    if not container.ready or container.retrieval is None:
        raise HTTPException(status_code=503, detail="检索服务未就绪")
    return await container.retrieval.search_materials(payload.query, top_k=payload.top_k)
