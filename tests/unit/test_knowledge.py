"""知识库解析、切片和入库服务测试。"""

from __future__ import annotations

import io

from src.knowledge.chunker import build_chunks
from src.knowledge.ingestion import KnowledgeIngestionService
from src.knowledge.models import (
    ChunkDraft,
    ChunkType,
    DocumentRecord,
    DocumentStatus,
    IngestionJobRecord,
)
from src.knowledge.parsers import DocumentParseError, parse_document
from tests.helpers import make_test_settings


def test_parse_markdown_keeps_heading_path() -> None:
    """Markdown 标题成为 heading_path，正文按段落保留。"""
    parsed = parse_document(
        "guide.md",
        "# 字幕\n\n字幕应与口播同步。\n\n## 导出\n\n导出前检查音画同步。".encode(),
    )
    assert parsed.title == "guide"
    assert parsed.blocks[0].heading_path == ["字幕"]
    assert parsed.blocks[1].heading_path == ["字幕", "导出"]


def test_parse_csv_one_record_per_row() -> None:
    """CSV 一行一个 record，并保留表头语义。"""
    parsed = parse_document("faq.csv", "问题,答案\n字幕怎么加,打开字幕入口\n".encode())
    assert len(parsed.blocks) == 1
    assert parsed.blocks[0].kind == "record"
    assert "问题：字幕怎么加" in parsed.blocks[0].text
    assert "答案：打开字幕入口" in parsed.blocks[0].text


def test_parse_json_array() -> None:
    """JSON 数组逐对象转为 record。"""
    parsed = parse_document(
        "faq.json",
        '[{"title":"字幕","body":"字幕应与口播同步"},{"title":"导出","body":"导出前检查"}]'.encode(),
    )
    assert len(parsed.blocks) == 2
    assert "title：字幕" in parsed.blocks[0].text


def test_parse_docx() -> None:
    """DOCX 标题样式与段落可解析。"""
    from docx import Document

    document = Document()
    document.add_heading("字幕设置", level=1)
    document.add_paragraph("字幕应与口播同步。")
    buffer = io.BytesIO()
    document.save(buffer)
    parsed = parse_document("guide.docx", buffer.getvalue())
    assert parsed.blocks
    assert parsed.blocks[0].heading_path == ["字幕设置"]


def test_scanned_pdf_without_text_raises() -> None:
    """没有文本层的 PDF 明确失败，不返回空文档。"""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    try:
        parse_document("scan.pdf", buffer.getvalue())
    except DocumentParseError as exc:
        assert "OCR" in str(exc) or "文本" in str(exc)
    else:
        raise AssertionError("empty PDF should fail")


def test_parent_child_chunking() -> None:
    """正文按句切 child，parent 聚合并保持 heading 边界。"""
    parsed = parse_document(
        "guide.md",
        "# 字幕\n\n字幕应与口播同步出现。每行字幕只表达一个意思。字幕断句应跟随口播停顿。\n\n## 导出\n\n导出前检查音画同步。".encode(),
    )
    chunks = build_chunks("doc-12345678", parsed)
    parents = [item for item in chunks if item.chunk_type is ChunkType.PARENT]
    children = [item for item in chunks if item.chunk_type is ChunkType.CHILD]
    assert len(parents) == 2
    assert len(children) == 4
    assert all(child.parent_id for child in children)
    assert all(child.material_id for child in children)
    assert parents[0].heading_path == ["字幕"]
    assert parents[1].heading_path == ["字幕", "导出"]
    ordinals = [item.ordinal for item in chunks]
    assert len(ordinals) == len(set(ordinals))


class FakeRepository:
    """记录发布与状态变化的入库仓库。"""

    def __init__(self) -> None:
        """输入：无；输出：空仓库；作用：捕获发布结果。"""
        self.published: tuple[list[ChunkDraft], list[list[float]]] | None = None
        self.document_updates: list[dict] = []
        self.job_updates: list[dict] = []

    async def update_document(self, document_id: str, **fields) -> None:
        self.document_updates.append(fields)

    async def update_job(self, job_id: str, **fields) -> None:
        self.job_updates.append(fields)

    async def publish(self, document_id, job_id, chunks, embeddings, *, source_name="") -> None:
        self.published = (chunks, embeddings)
        self.document_updates.append({"status": DocumentStatus.READY.value})
        self.job_updates.append({"status": DocumentStatus.READY.value})


class FakeEmbedder:
    """确定性的批量 embedding。"""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 1024 for _ in texts]


async def test_ingestion_publishes_parent_child() -> None:
    """TXT 入库：READY、parent/child 数量正确、embedding 数量匹配。"""
    repository = FakeRepository()
    service = KnowledgeIngestionService(
        make_test_settings(dashscope_embedding_model="text-embedding-v3"),
        repository,
        FakeEmbedder(),
    )
    document = DocumentRecord(
        id="doc-12345678",
        filename="guide.txt",
        mime="text/plain",
        size_bytes=10,
        content_hash="hash",
        status=DocumentStatus.QUEUED.value,
    )
    job = IngestionJobRecord(id="job-1", document_id=document.id, status="QUEUED")
    await service.ingest(
        document,
        job,
        "字幕应与口播同步出现。每行字幕只表达一个意思。".encode(),
    )
    assert repository.published is not None
    chunks, vectors = repository.published
    parents = [item for item in chunks if item.chunk_type is ChunkType.PARENT]
    children = [item for item in chunks if item.chunk_type is ChunkType.CHILD]
    assert len(parents) == 1
    assert len(children) == 2
    assert len(vectors) == 2
    assert repository.document_updates[-1]["status"] == DocumentStatus.READY.value


async def test_ingestion_failure_marks_document_failed() -> None:
    """解析失败：文档 FAILED，不发布任何块。"""
    repository = FakeRepository()
    service = KnowledgeIngestionService(make_test_settings(), repository, FakeEmbedder())
    document = DocumentRecord(
        id="doc-failed",
        filename="scan.pdf",
        mime="application/pdf",
        size_bytes=1,
        content_hash="hash2",
        status=DocumentStatus.QUEUED.value,
    )
    job = IngestionJobRecord(id="job-2", document_id=document.id, status="QUEUED")
    await service.ingest(document, job, b"not-a-pdf")
    assert repository.published is None
    assert repository.document_updates[-1]["status"] == DocumentStatus.FAILED.value
    assert repository.job_updates[-1]["status"] == DocumentStatus.FAILED.value
