"""图节点依赖容器。

输入：模型、检索、规则、时长与校验实现。
输出：`GraphServices`，由 build 阶段注入节点闭包。
作用：让真实运行和测试使用同一套图逻辑。
实现：使用 dataclass + 可替换的 callable/Protocol。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from src.config import Settings
from src.domain.models import Brief, CheckReport, Claim, DurationReport, Material, Script, Violation
from src.domain.validators import validate_output
from src.llm.base import DecisionModel, WriterModel
from src.tools.duration import estimate_duration_tool
from src.tools.retrieval import RetrievalTool
from src.tools.rule_repository import RuleProvider, default_rule_provider
from src.tools.rules import check_rules

DurationEstimator = Callable[[Script, int, int], DurationReport | Awaitable[DurationReport]]
RuleChecker = Callable[..., CheckReport]
OutputValidator = Callable[..., CheckReport]


@dataclass
class GraphServices:
    """节点依赖集合。"""

    settings: Settings
    decision_model: DecisionModel
    writer_model: WriterModel
    retrieval: RetrievalTool
    rule_provider: RuleProvider = field(default_factory=default_rule_provider)
    duration_estimator: DurationEstimator = estimate_duration_tool
    rule_checker: RuleChecker = check_rules
    output_validator: OutputValidator = validate_output

    async def get_duration(self, script: Script, target_sec: int, tolerance_sec: int) -> DurationReport:
        """输入：脚本/目标/容差；输出：时长报告；作用：兼容同步与异步替身；实现：检测 awaitable。"""
        result = self.duration_estimator(script, target_sec, tolerance_sec)
        if hasattr(result, "__await__"):
            return await result  # type: ignore[misc]
        return result

    def make_writer_constraints(self, brief: Brief) -> list[str]:
        """输入：Brief；输出：Writer 必须遵守的文本约束；作用：把规则带进写作上下文。"""
        constraints = ["只能引用 approved claims", "口播必须包含 claim 原文"]
        if brief.platform:
            constraints.append(f"平台：{brief.platform}")
        if brief.target_duration_sec:
            constraints.append(f"目标时长：{brief.target_duration_sec} 秒")
        constraints.extend(["禁止绝对化用语", "无效果数据不得写效果结论"])
        return constraints


def violation_payload(violations: list[Violation]) -> list[dict[str, Any]]:
    """输入：违规列表；输出：可 JSON 序列化列表；作用：事件/Trace 使用；实现：Pydantic dump。"""
    return [item.model_dump(mode="json") for item in violations]


def source_payload(script: Script, claims: list[Claim], materials: list[Material]) -> list[dict[str, Any]]:
    """输入：脚本/声明/素材；输出：被引用来源摘要；作用：最终结果可追溯。"""
    claim_ids = {claim_id for shot in script.shots for claim_id in shot.claim_ids}
    material_by_id = {material.id: material for material in materials}
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for claim in claims:
        if claim.claim_id not in claim_ids or claim.source_id in seen:
            continue
        seen.add(claim.source_id)
        material = material_by_id.get(claim.source_id)
        sources.append(
            {
                "source_id": claim.source_id,
                "claim_id": claim.claim_id,
                "text": claim.text,
                "source": material.source if material else "",
                "source_type": material.source_type if material else "",
            }
        )
    return sources
