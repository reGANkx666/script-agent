"""模型适配接口。

输入：图节点投影后的上下文。
输出：下一步动作或脚本草稿。
作用：隔开 DeepSeek 实现与测试 Mock，保证图路由不依赖具体模型。
实现：使用 Protocol + Pydantic 上下文，任何适配器都必须遵守同一契约。
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from src.domain.models import (
    ActionDecision,
    Brief,
    CheckReport,
    Claim,
    ContextBlock,
    DurationReport,
    Material,
    Script,
    Violation,
)


class ModelError(RuntimeError):
    """模型输出不可用或凭据不可用。"""


class DecisionContext(BaseModel):
    """Agent 决策所需的最小投影，不包含全量 Trace 或密钥。"""

    brief: Brief
    materials: list[Material] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    draft: dict[str, Any] | None = None
    duration: DurationReport | None = None
    check: CheckReport | None = None
    step: int = 0
    tool_calls: int = 0
    rewrite_count: int = 0
    revision_count: int = 0
    max_steps: int = 8
    max_tool_calls: int = 12
    max_revisions: int = 2
    max_rewrites: int = 2
    last_observation: dict[str, Any] | None = None
    previous_queries: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)


class WriterContext(BaseModel):
    """Writer 所需的最小事实上下文。"""

    brief: Brief
    claims: list[Claim] = Field(default_factory=list)
    materials: list[Material] = Field(default_factory=list)
    contexts: list[ContextBlock] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    previous_script: Script | None = None
    violations: list[Violation] = Field(default_factory=list)
    revision: int = 0


class DecisionModel(Protocol):
    """动作选择模型接口。"""

    async def decide(self, context: DecisionContext) -> ActionDecision:
        """输入：决策投影；输出：唯一动作；作用：驱动 Agent 路由。"""
        ...


class WriterModel(Protocol):
    """脚本生成/修订模型接口。"""

    async def write(self, context: WriterContext) -> Script:
        """输入：事实上下文；输出：脚本草稿；作用：生成或修订分镜。"""
        ...


def decision_context_payload(context: DecisionContext) -> dict[str, Any]:
    """输入：决策上下文；输出：可 JSON 序列化字典；作用：构造 Prompt；实现：排除空字段。"""
    return context.model_dump(mode="json", exclude_none=True)


def writer_context_payload(context: WriterContext) -> dict[str, Any]:
    """输入：Writer 上下文；输出：可 JSON 序列化字典；作用：构造 Prompt。"""
    return context.model_dump(mode="json", exclude_none=True)
