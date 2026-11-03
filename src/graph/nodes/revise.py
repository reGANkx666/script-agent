"""脚本修订节点。"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from src.domain.models import Violation
from src.graph.nodes.common import emit
from src.graph.services import GraphServices
from src.graph.state import AgentState
from src.llm.base import WriterContext


def make_revise_node(services: GraphServices):
    """输入：依赖容器；输出：revise_script 节点。"""

    async def revise_script(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        """按违规与时长差异修订脚本。

        输入：旧 draft、check/duration 违规。
        输出：新 draft（重置旧 duration/check）、revision_count、tool_calls。
        作用：把修复反馈显式传给 Writer，并在预算内迭代。
        实现：合并规则违规与时长违规；Writer 输出再次做 claim 白名单检查。
        """
        draft = state.get("draft")
        brief = state["brief"]
        revision_count = int(state.get("revision_count", 0))
        max_revisions = services.settings.max_revisions
        tool_calls = int(state.get("tool_calls", 0)) + 1
        if draft is None:
            observation = {
                "action": "revise_script",
                "status": "ERROR",
                "error": "没有可修订的脚本",
                "error_status": "MODEL_ERROR",
            }
            await emit(config, "tool.result", "revise_script", observation)
            return {
                "tool_calls": tool_calls,
                "pending_action": None,
                "last_observation": observation,
            }
        if revision_count >= max_revisions:
            observation = {
                "action": "revise_script",
                "status": "ERROR",
                "error": "修订次数已达上限",
                "error_status": "UNSATISFIABLE_RULES",
            }
            await emit(config, "tool.result", "revise_script", observation)
            return {
                "tool_calls": tool_calls,
                "pending_action": None,
                "last_observation": observation,
            }

        violations = _collect_violations(state)
        claims = [claim for claim in state.get("claims", []) if claim.approved]
        await emit(
            config,
            "tool.called",
            "revise_script",
            {
                "revision": revision_count + 1,
                "violation_codes": [item.code for item in violations],
            },
        )
        try:
            script = await services.writer_model.write(
                WriterContext(
                    brief=brief,
                    claims=claims,
                    materials=list(state.get("materials", [])),
                    contexts=list(state.get("contexts", [])),
                    constraints=services.make_writer_constraints(brief),
                    previous_script=draft,
                    violations=violations,
                    revision=revision_count + 1,
                )
            )
            allowed = {claim.claim_id for claim in claims}
            used = {claim_id for shot in script.shots for claim_id in shot.claim_ids}
            if not used.issubset(allowed):
                raise ValueError("script references unknown claims")
        except Exception as exc:
            observation = {
                "action": "revise_script",
                "status": "ERROR",
                "error": f"Writer 修订失败：{type(exc).__name__}",
                "error_status": "MODEL_ERROR",
            }
            await emit(
                config,
                "tool.result",
                "revise_script",
                {"status": "ERROR", "error_type": type(exc).__name__},
            )
            return {
                "tool_calls": tool_calls,
                "pending_action": None,
                "last_observation": observation,
            }

        observation = {
            "action": "revise_script",
            "status": "OK",
            "script_id": script.script_id,
            "revision": revision_count + 1,
            "shot_count": len(script.shots),
        }
        await emit(config, "tool.result", "revise_script", observation)
        return {
            "draft": script,
            "duration": None,
            "check": None,
            "revision_count": revision_count + 1,
            "tool_calls": tool_calls,
            "pending_action": None,
            "last_observation": observation,
        }

    return revise_script


def _collect_violations(state: AgentState) -> list[Violation]:
    """输入：状态；输出：需要 Writer 修复的违规；作用：统一规则与时长反馈。

    实现：复制 check 违规；若时长未通过且没有 DURATION_OVER，补一条合成违规。
    """
    check = state.get("check")
    violations = list(check.violations) if check else []
    duration = state.get("duration")
    if duration is not None and not duration.passed and not any(
        item.code in {"DURATION_OVER", "DURATION_TARGET_MISMATCH"} for item in violations
    ):
        violations.append(
            Violation(
                code="DURATION_OVER",
                severity="block",
                message="总时长超出目标容差",
                evidence={
                    "total_sec": duration.total_sec,
                    "target_sec": duration.target_sec,
                    "tolerance_sec": duration.tolerance_sec,
                },
                suggested_fix="压缩口播或调整分镜",
            )
        )
    return violations
