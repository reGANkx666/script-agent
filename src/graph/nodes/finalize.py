"""最终输出节点。"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from src.domain.models import RunResult, Violation
from src.domain.status import TaskStatus
from src.graph.nodes.common import emit
from src.graph.services import GraphServices, source_payload
from src.graph.state import AgentState


def make_finalize_node(services: GraphServices):
    """输入：依赖容器；输出：finalize 节点。"""

    async def finalize(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        """执行最终不变量校验并输出结果。

        输入：完整状态。
        输出：result、status。
        作用：只有规则与输出校验都通过才返回 SUCCESS。
        实现：补齐缺失检查 → `validate_output` → 汇总来源/风险/违规 → 终态事件。
        """
        draft = state.get("draft")
        brief = state["brief"]
        duration = state.get("duration")
        check = state.get("check")
        target = brief.target_duration_sec
        violations: list[Violation] = []
        if draft is None:
            violations.append(
                Violation(code="DRAFT_MISSING", severity="block", message="缺少脚本草稿")
            )
            report_passed = False
        else:
            if check is None:
                violations.append(
                    Violation(code="CHECK_MISSING", severity="block", message="缺少规则检查")
                )
            validation = services.output_validator(
                draft,
                list(state.get("claims", [])),
                list(state.get("materials", [])),
                duration,
                check,
                target_sec=target,
                tolerance_sec=2,
            )
            violations = [*violations, *validation.violations]
            report_passed = validation.passed and check is not None and check.passed

        if report_passed and draft is not None:
            status = TaskStatus.SUCCESS
        elif any(item.code.startswith("DURATION") for item in violations):
            status = TaskStatus.UNSATISFIABLE_DURATION
        else:
            status = TaskStatus.UNSATISFIABLE_RULES

        sources = (
            source_payload(draft, list(state.get("claims", [])), list(state.get("materials", [])))
            if draft is not None
            else []
        )
        risks = _merge_final_risks(list(state.get("risks", [])), violations)
        result = RunResult(
            status=str(status),
            script=draft,
            sources=sources,
            estimated_duration_sec=duration.total_sec if duration else None,
            target_duration_sec=target,
            risks=risks,
            violations=violations,
            attempts={
                "steps": int(state.get("step", 0)),
                "tool_calls": int(state.get("tool_calls", 0)),
                "revisions": int(state.get("revision_count", 0)),
                "rewrites": int(state.get("rewrite_count", 0)),
            },
            suggestions=[
                item.suggested_fix
                for item in violations
                if item.suggested_fix and item.severity == "block"
            ],
        ).model_dump(mode="json")

        if status is TaskStatus.SUCCESS:
            await emit(
                config,
                "run.completed",
                "finalize",
                {
                    "status": str(status),
                    "sources": [item["source_id"] for item in sources],
                    "estimated_duration_sec": result["estimated_duration_sec"],
                },
            )
        else:
            await emit(
                config,
                "run.failed",
                "finalize",
                {
                    "status": str(status),
                    "violations": [item.code for item in violations],
                },
            )
        return {
            "result": result,
            "status": str(status),
            "check": state.get("check"),
            "pending_action": None,
            "error": (
                None
                if status is TaskStatus.SUCCESS
                else {"status": str(status), "message": "最终输出未通过不变量校验"}
            ),
        }

    return finalize


def _merge_final_risks(risks: list[dict[str, Any]], violations: list[Violation]) -> list[dict[str, Any]]:
    """输入：风险与最终违规；输出：去重风险；作用：保证 warn/info 可见。"""
    merged = list(risks)
    seen = {(item.get("code"), item.get("shot_id"), item.get("field")) for item in merged}
    for violation in violations:
        if violation.severity == "block":
            continue
        key = (violation.code, violation.shot_id, None)
        if key in seen:
            continue
        seen.add(key)
        merged.append(
            {
                "code": violation.code,
                "severity": violation.severity,
                "shot_id": violation.shot_id,
                "message": violation.message,
            }
        )
    return merged
