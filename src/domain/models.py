"""Pydantic 领域模型。

输入：API 请求、模型输出、工具结果与数据库种子数据。
输出：强类型对象；所有跨层数据都先经过这里校验。
作用：定义 Brief/Script/Claim/规则报告等共享契约。
实现：字段与 `implementation-plan.md` 对齐；集合字段使用 factory，禁止共享可变默认值。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from src.domain.status import ActionName


class Brief(BaseModel):
    """用户创作需求。"""

    topic: str = Field(min_length=1)
    audience: str | None = None
    platform: str | None = None
    target_duration_sec: int | None = Field(default=None, gt=0)
    style: str | None = None

    @field_validator("topic")
    @classmethod
    def _strip_topic(cls, value: str) -> str:
        """输入：原始主题；输出：去空白主题；作用：阻止纯空白主题进入图。"""
        value = value.strip()
        if not value:
            raise ValueError("topic 不能为空")
        return value

    @field_validator("platform", "audience", "style")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        """输入：可选字符串；输出：去空白或 None；作用：统一空值语义。"""
        if value is None:
            return None
        value = value.strip()
        return value or None


class Material(BaseModel):
    """可引用的素材条目。"""

    id: str
    text: str
    source: str
    source_type: str = "product_brief"
    scope: str | None = None
    tags: list[str] = Field(default_factory=list)
    document_id: str | None = None
    chunk_id: str | None = None
    parent_id: str | None = None
    parent_text: str | None = None
    heading_path: list[str] = Field(default_factory=list)


class ContextBlock(BaseModel):
    """Parent chunk 投影，只用于 Writer 理解上下文，不作为引用来源。"""

    parent_id: str
    text: str
    heading_path: list[str] = Field(default_factory=list)
    source: str = ""
    source_ids: list[str] = Field(default_factory=list)


class Claim(BaseModel):
    """脚本可使用的、带来源的事实声明。"""

    claim_id: str
    text: str
    source_id: str
    approved: bool = True


class Shot(BaseModel):
    """单个分镜。"""

    shot_id: str
    visual: str
    narration: str
    subtitle: str
    duration_sec: int = Field(default=0, ge=0)
    visual_hold_sec: int = Field(default=0, ge=0)
    claim_ids: list[str] = Field(default_factory=list)


class Script(BaseModel):
    """待校验脚本。"""

    script_id: str
    title: str
    shots: list[Shot] = Field(min_length=1)


class DurationReport(BaseModel):
    """时长估算结果。"""

    total_sec: int
    target_sec: int
    tolerance_sec: int
    passed: bool
    per_shot: list[dict[str, Any]] = Field(default_factory=list)
    basis: str


class Violation(BaseModel):
    """规则或输出校验违规。"""

    code: str
    severity: Literal["block", "warn", "info"]
    shot_id: str | None = None
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    suggested_fix: str | None = None


class CheckReport(BaseModel):
    """规则检查汇总。"""

    passed: bool
    violations: list[Violation] = Field(default_factory=list)

    @property
    def has_block(self) -> bool:
        """输出：是否存在 block 级违规；作用：统一路由与终态判断。"""
        return any(item.severity == "block" for item in self.violations)


class SearchHit(BaseModel):
    """混合检索单条结果。"""

    source_id: str
    text: str
    score: float
    source: str = ""
    source_type: str = "product_brief"
    matched_terms: list[str] = Field(default_factory=list)
    bm25_rank: int | None = None
    vector_rank: int | None = None
    parent_id: str | None = None
    parent_text: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    rerank_score: float | None = None


class SearchResult(BaseModel):
    """检索工具返回值，状态必须显式，不能静默降级。"""

    status: Literal["OK", "EMPTY", "LOW_SCORE", "ERROR"]
    query: str
    results: list[SearchHit] = Field(default_factory=list)
    contexts: list[ContextBlock] = Field(default_factory=list)
    debug: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    degraded: bool = False
    risk: dict[str, Any] | None = None


class ActionDecision(BaseModel):
    """模型选择的下一步动作。"""

    action: ActionName
    args: dict[str, Any] = Field(default_factory=dict)
    basis: str = ""

    @field_validator("basis")
    @classmethod
    def _basis_not_secret(cls, value: str) -> str:
        """输入：决策依据；输出：截断后的依据；作用：避免 Trace 膨胀。"""
        return value.strip()[:500]


class PendingQuestion(BaseModel):
    """需要用户补充的信息。"""

    type: Literal["NEEDS_INPUT"] = "NEEDS_INPUT"
    missing: list[str] = Field(default_factory=list)
    question: str


class RuleSet(BaseModel):
    """品牌/平台规则集合。"""

    brand: list[dict[str, Any]] = Field(default_factory=list)
    platform: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


class RunResult(BaseModel):
    """成功或可解释终态的输出摘要。"""

    status: str
    script: Script | None = None
    sources: list[dict[str, Any]] = Field(default_factory=list)
    estimated_duration_sec: int | None = None
    target_duration_sec: int | None = None
    risks: list[dict[str, Any]] = Field(default_factory=list)
    violations: list[Violation] = Field(default_factory=list)
    attempts: dict[str, int] = Field(default_factory=dict)
    suggestions: list[str] = Field(default_factory=list)
