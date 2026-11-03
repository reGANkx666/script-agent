"""显式失败节点。"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from src.domain.models import RunResult
from src.domain.status import FAILURE_STATUSES, TaskStatus
from src.graph.nodes.common import emit
from src.graph.services import GraphServices, source_payload
from src.graph.state import AgentState


def make_fail_node(services: GraphServices):
    """输入：依赖容器；输出：fail 节点。"""

    async def fail(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        """以可解释状态终止运行。

        输入：pending_action.args 中的 status/message/suggestions。
        输出：result、status、error。
        作用：无素材、工具失败、超时长、规则不可满足都必须显式结束，不能编造成功。
        实现：校验/归一状态；保留已有脚本与违规作为诊断；发送 run.failed。
        """
        action = state.get("pending_action")
        args = action.args if action else {}
        raw_status = str(args.get("status", TaskStatus.TOOL_ERROR))
        status = _normalize_failure(raw_status)
        message = str(args.get("message") or _default_message(status))
        suggestions = [str(item) for item in args.get("suggestions", []) if str(item).strip()]
        draft = state.get("draft")
        duration = state.get("duration")
        check = state.get("check")
        violations = list(check.violations) if check else []
        sources = (
            source_payload(draft, list(state.get("claims", [])), list(state.get("materials", [])))
            if draft is not None
            else []
        )
        result = RunResult(
            status=str(status),
            script=draft,
            sources=sources,
            estimated_duration_sec=duration.total_sec if duration else None,
            target_duration_sec=state["brief"].target_duration_sec,
            risks=list(state.get("risks", [])),
            violations=violations,
            attempts={
                "steps": int(state.get("step", 0)),
                "tool_calls": int(state.get("tool_calls", 0)),
                "revisions": int(state.get("revision_count", 0)),
                "rewrites": int(state.get("rewrite_count", 0)),
            },
            suggestions=suggestions or _default_suggestions(status),
        ).model_dump(mode="json")
        await emit(
            config,
            "run.failed",
            "fail",
            {
                "status": str(status),
                "message": message,
                "steps": result["attempts"]["steps"],
            },
        )
        return {
            "status": str(status),
            "error": {"status": str(status), "message": message},
            "result": result,
            "pending_action": None,
            "pending_question": None,
            "last_observation": {
                "action": "fail",
                "status": str(status),
                "message": message,
            },
        }

    return fail


def _normalize_failure(value: str) -> TaskStatus:
    """输入：模型/编排器状态字符串；输出：合法失败状态；作用：避免非法状态入库。

    实现：只接受失败枚举，未知值回退 TOOL_ERROR。
    """
    try:
        status = TaskStatus(value)
    except ValueError:
        return TaskStatus.TOOL_ERROR
    return status if status in FAILURE_STATUSES else TaskStatus.TOOL_ERROR


def _default_message(status: TaskStatus) -> str:
    """输入：失败状态；输出：默认文案；作用：保证结果可解释。"""
    return {
        TaskStatus.NO_MATERIAL: "没有检索到可引用素材",
        TaskStatus.TOOL_ERROR: "工具连续失败，无法继续",
        TaskStatus.UNSATISFIABLE_DURATION: "无法在目标时长内满足内容约束",
        TaskStatus.UNSATISFIABLE_RULES: "无法满足品牌或平台规则",
        TaskStatus.MODEL_ERROR: "模型输出不可用",
        TaskStatus.MAX_STEPS: "达到步数或工具调用预算",
    }.get(status, "运行失败")


def _default_suggestions(status: TaskStatus) -> list[str]:
    """输入：失败状态；输出：可操作建议；作用：让失败结果对用户有用。"""
    return {
        TaskStatus.NO_MATERIAL: ["补充产品资料", "换一个更宽泛的主题"],
        TaskStatus.TOOL_ERROR: ["稍后重试", "检查 embedding 与数据库连接"],
        TaskStatus.UNSATISFIABLE_DURATION: ["放宽目标时长", "减少分镜或口播字数"],
        TaskStatus.UNSATISFIABLE_RULES: ["人工检查违规项", "更新品牌规则或素材"],
        TaskStatus.MODEL_ERROR: ["检查模型配置后重试"],
        TaskStatus.MAX_STEPS: ["简化需求或提高预算"],
    }.get(status, [])
