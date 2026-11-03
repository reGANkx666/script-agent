"""Agent 决策节点。"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from src.config import Settings
from src.domain.models import ActionDecision, Claim
from src.domain.status import ActionName, TaskStatus
from src.graph.nodes.common import (
    approved_claims,
    emit,
    script_summary,
)
from src.graph.services import GraphServices
from src.graph.state import AgentState
from src.llm.base import DecisionContext, ModelError


def make_agent_node(services: GraphServices):
    """输入：依赖容器；输出：唯一的模型决策节点。"""

    async def agent(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        """选择下一步动作。

        输入：当前业务状态。
        输出：pending_action、step 与本轮观察。
        作用：图的唯一模型决策点；所有工具执行由后续节点完成。
        实现：预算护栏 → 最小上下文投影 → 模型输出校验 → 安全兜底。
        """
        settings = services.settings
        step = int(state.get("step", 0))
        tool_calls = int(state.get("tool_calls", 0))
        if step >= settings.max_steps or tool_calls >= settings.max_tool_calls:
            return _forced_failure(
                state,
                TaskStatus.MAX_STEPS,
                f"达到预算上限：step={step}, tool_calls={tool_calls}",
                step=step,
            )

        context = _build_context(state, settings)
        try:
            decision = await services.decision_model.decide(context)
        except ModelError as exc:
            return _forced_failure(
                state,
                TaskStatus.MODEL_ERROR,
                f"模型动作不可用：{type(exc).__name__}",
                step=step,
            )

        decision = _apply_safety_guards(decision, state, settings)
        await emit(
            config,
            "action.selected",
            "agent",
            {
                "action": str(decision.action),
                "args": decision.args,
                "basis": decision.basis,
                "step": step + 1,
            },
        )
        return {
            "pending_action": decision,
            "step": step + 1,
            "status": "RUNNING",
        }

    return agent


def _build_context(state: AgentState, settings: Settings) -> DecisionContext:
    """输入：图状态与配置；输出：最小决策上下文；作用：控制 Prompt 大小且不泄漏事件历史。"""
    return DecisionContext(
        brief=state["brief"],
        materials=state.get("materials", []),
        claims=state.get("claims", []),
        draft=script_summary(state.get("draft")),
        duration=state.get("duration"),
        check=state.get("check"),
        step=int(state.get("step", 0)),
        tool_calls=int(state.get("tool_calls", 0)),
        rewrite_count=int(state.get("rewrite_count", 0)),
        revision_count=int(state.get("revision_count", 0)),
        max_steps=settings.max_steps,
        max_tool_calls=settings.max_tool_calls,
        max_revisions=settings.max_revisions,
        max_rewrites=settings.max_rewrites,
        last_observation=state.get("last_observation"),
        previous_queries=list(state.get("searched_queries", [])),
        missing_fields=list(state.get("missing_fields", [])),
    )


def _apply_safety_guards(
    decision: ActionDecision, state: AgentState, settings: Settings
) -> ActionDecision:
    """输入：模型动作与状态；输出：安全兜底后的动作。

    作用：把预算耗尽、无素材、重复修订等不可恢复情况转成显式终态。
    实现：只覆盖明确的战术性错误，不替代正常模型选择。
    """
    action = decision.action
    claims: list[Claim] = approved_claims(state)
    revision_count = int(state.get("revision_count", 0))
    rewrite_count = int(state.get("rewrite_count", 0))
    max_revisions = settings.max_revisions
    duration = state.get("duration")
    check = state.get("check")

    last_observation = state.get("last_observation") or {}
    if last_observation.get("status") == "ERROR" and action is not ActionName.FAIL:
        error_status = last_observation.get("error_status") or TaskStatus.TOOL_ERROR.value
        try:
            failure = TaskStatus(str(error_status))
        except ValueError:
            failure = TaskStatus.TOOL_ERROR
        if failure not in {
            TaskStatus.TOOL_ERROR,
            TaskStatus.MODEL_ERROR,
            TaskStatus.NO_MATERIAL,
            TaskStatus.UNSATISFIABLE_RULES,
            TaskStatus.UNSATISFIABLE_DURATION,
        }:
            failure = TaskStatus.TOOL_ERROR
        return ActionDecision(
            action=ActionName.FAIL,
            args={
                "status": failure.value,
                "message": str(last_observation.get("error") or "工具或模型输出不可用"),
                "suggestions": [],
            },
            basis="安全护栏：上一步执行失败，禁止继续生成",
        )

    if (
        action is ActionName.SEARCH_MATERIALS
        and last_observation.get("status") in {"EMPTY", "LOW_SCORE"}
        and rewrite_count >= settings.max_rewrites
    ):
        return ActionDecision(
            action=ActionName.FAIL,
            args={
                "status": TaskStatus.NO_MATERIAL.value,
                "message": "检索重写预算已用尽，仍没有可用素材",
                "suggestions": ["补充产品资料", "换一个更宽泛的主题"],
            },
            basis="安全护栏：检索重写预算耗尽",
        )

    if action is ActionName.DRAFT_SCRIPT and not claims:
        return ActionDecision(
            action=ActionName.FAIL,
            args={
                "status": TaskStatus.NO_MATERIAL.value,
                "message": "没有可用事实来源，不能生成产品脚本",
                "suggestions": ["补充产品资料后重试", "更换主题或查询词"],
            },
            basis="安全护栏：无 approved claim",
        )
    if action is ActionName.REVISE_SCRIPT and revision_count >= max_revisions:
        if duration is not None and not duration.passed:
            status = TaskStatus.UNSATISFIABLE_DURATION
            message = "修订次数已用尽，仍无法满足目标时长"
        else:
            status = TaskStatus.UNSATISFIABLE_RULES
            message = "修订次数已用尽，仍存在阻断规则"
        return ActionDecision(
            action=ActionName.FAIL,
            args={"status": status.value, "message": message, "suggestions": ["放宽时长或人工修订"]},
            basis="安全护栏：修订预算耗尽",
        )
    if (
        action is ActionName.FINALIZE
        and check is not None
        and check.has_block
        and revision_count >= max_revisions
    ):
        return ActionDecision(
            action=ActionName.FAIL,
            args={
                "status": TaskStatus.UNSATISFIABLE_RULES.value,
                "message": "存在无法通过修订消除的 block 规则违规",
                "suggestions": ["人工检查品牌规则与事实来源"],
            },
            basis="安全护栏：block 违规且修订预算耗尽",
        )
    return decision


def _forced_failure(
    state: AgentState, status: TaskStatus, message: str, step: int
) -> dict[str, Any]:
    """输入：状态、状态码、消息；输出：强制 fail 动作；作用：预算/模型异常显式终止。"""
    return {
        "pending_action": ActionDecision(
            action=ActionName.FAIL,
            args={"status": status.value, "message": message},
            basis="编排器安全护栏",
        ),
        "step": step,
        "status": "RUNNING",
        "last_observation": {
            "action": "agent",
            "selected": "fail",
            "status": status.value,
            "message": message,
        },
    }
