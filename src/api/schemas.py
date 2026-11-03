"""API 请求/响应模型。

输入：HTTP JSON。
输出：FastAPI 自动校验后的对象与 OpenAPI 文档。
作用：稳定对外契约，避免路由直接接受任意 dict。
实现：复用领域模型与场景模型；响应字段保持可读摘要。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.domain.models import Brief
from src.runtime.scenarios import Assertions, ScenarioSpec


class RunCreateRequest(BaseModel):
    """创建运行请求。"""

    brief: Brief


class RunLinks(BaseModel):
    """运行相关链接。"""

    self: str
    events: str
    result: str


class RunCreateResponse(BaseModel):
    """创建运行响应。"""

    run_id: str
    status: str
    links: RunLinks


class RunStatusResponse(BaseModel):
    """运行状态响应。"""

    run_id: str
    status: str
    current_node: str | None = None
    step: int = 0
    missing_fields: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ResumeRequest(BaseModel):
    """恢复运行请求。"""

    answer: dict[str, Any]


class ResumeResponse(BaseModel):
    """恢复运行响应。"""

    run_id: str
    status: str


class CancelResponse(BaseModel):
    """取消运行响应。"""

    run_id: str
    status: str


class TestRunRequest(BaseModel):
    """同步测试场景请求。"""

    mode: Literal["mock"] = "mock"
    brief: Brief
    scenario: ScenarioSpec
    assertions: Assertions = Field(default_factory=Assertions)


class HealthResponse(BaseModel):
    """健康检查响应。"""

    status: str
    app_env: str
    errors: list[str] = Field(default_factory=list)


class ReadinessResponse(BaseModel):
    """就绪检查响应。"""

    status: str
    ready: bool
    errors: list[str] = Field(default_factory=list)
    details: dict[str, str] = Field(default_factory=dict)


class TraceResponse(BaseModel):
    """Trace 响应。"""

    run_id: str
    trace: list[dict[str, Any]]


class KnowledgeDocumentResponse(BaseModel):
    """知识库文档摘要。"""

    id: str
    filename: str
    mime: str
    size_bytes: int
    status: str
    error: str | None = None
    title: str | None = None
    active: bool = False
    parent_count: int = 0
    child_count: int = 0
    content_hash: str = ""
    created_at: Any = None
    updated_at: Any = None


class KnowledgeJobResponse(BaseModel):
    """入库任务状态。"""

    id: str
    document_id: str
    status: str
    stage: str
    progress: int
    error: str | None = None
    created_at: Any = None
    updated_at: Any = None


class KnowledgeChunkResponse(BaseModel):
    """Parent/Child 块预览。"""

    id: str
    document_id: str
    parent_id: str | None = None
    chunk_type: str
    ordinal: int
    heading_path: list[str] = Field(default_factory=list)
    text: str
    token_count: int = 0
    active: bool = True
    material_id: str | None = None


class KnowledgeUploadResponse(BaseModel):
    """上传响应。"""

    document: KnowledgeDocumentResponse
    job: KnowledgeJobResponse | None = None
    deduplicated: bool = False


class KnowledgeSearchRequest(BaseModel):
    """知识库调试检索请求。"""

    query: str = Field(min_length=1)
    top_k: int = Field(default=8, ge=1, le=30)
