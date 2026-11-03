"""知识库领域模型。

输入：解析器、切片器、入库任务与 API。
输出：不依赖数据库的强类型中间结构。
作用：把“文档 → 块 → 素材”的边界固定下来，避免解析/入库逻辑互相耦合。
实现：dataclass + 显式状态枚举；不把 embedding 放进内容模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DocumentStatus(StrEnum):
    """文档入库状态。"""

    QUEUED = "QUEUED"
    PARSING = "PARSING"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    READY = "READY"
    FAILED = "FAILED"
    INACTIVE = "INACTIVE"


class ChunkType(StrEnum):
    """块类型。"""

    PARENT = "parent"
    CHILD = "child"


@dataclass
class ParsedBlock:
    """解析后的结构块。"""

    text: str
    heading_path: list[str] = field(default_factory=list)
    kind: str = "paragraph"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedDocument:
    """一次上传解析结果。"""

    title: str
    blocks: list[ParsedBlock] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChunkDraft:
    """尚未落库的 parent/child 块。"""

    id: str
    document_id: str
    chunk_type: ChunkType
    ordinal: int
    text: str
    parent_id: str | None = None
    heading_path: list[str] = field(default_factory=list)
    material_id: str | None = None
    embedding_text: str | None = None
    token_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentRecord:
    """文档元数据。"""

    id: str
    filename: str
    mime: str
    size_bytes: int
    content_hash: str
    status: str
    error: str | None = None
    chunker_version: str = "pc-v1"
    embedding_model: str = ""
    title: str | None = None
    storage_path: str | None = None
    active: bool = False
    created_at: Any = None
    updated_at: Any = None
    parent_count: int = 0
    child_count: int = 0


@dataclass
class IngestionJobRecord:
    """入库任务状态。"""

    id: str
    document_id: str
    status: str
    stage: str = "QUEUED"
    progress: int = 0
    error: str | None = None
    created_at: Any = None
    updated_at: Any = None


@dataclass
class ChunkRecord:
    """已落库块，用于预览。"""

    id: str
    document_id: str
    parent_id: str | None
    chunk_type: str
    ordinal: int
    heading_path: list[str]
    text: str
    token_count: int
    active: bool
    material_id: str | None = None
