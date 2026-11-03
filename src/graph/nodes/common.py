"""图节点共享辅助。

输入：状态、配置、领域对象。
输出：事件、缺失字段、默认值、脚本摘要。
作用：避免各节点重复实现横切逻辑。
实现：纯函数 + 一个可选的异步事件发送器。
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from langchain_core.runnables import RunnableConfig

from src.domain.models import Brief, Material, Script
from src.graph.state import AgentState

logger = logging.getLogger(__name__)

DEFAULT_AUDIENCE = "短视频入门用户"
DEFAULT_STYLE = "轻快、口语化"

_REQUIRED_FIELDS = ("platform", "target_duration_sec")


async def _maybe_await(value: Any) -> Any:
    """输入：任意值；输出：await 后的值；作用：兼容同步/异步回调；实现：检查 awaitable。"""
    if inspect.isawaitable(value):
        return await value
    return value


async def emit(config: RunnableConfig | None, event_type: str, node: str, payload: dict[str, Any]) -> None:
    """发送自定义事件。

    输入：LangGraph config、事件类型、节点名、载荷。
    输出：无。
    作用：把节点内部动作暴露给 RunManager / SSE，而不写数据库。
    实现：读取 `configurable.event_sink`；事件发送失败只记录日志，不破坏业务执行。
    """
    sink = (config or {}).get("configurable", {}).get("event_sink")
    if not sink:
        return
    try:
        await _maybe_await(
            sink({"event_type": event_type, "node": node, "payload": payload})
        )
    except Exception as exc:  # 观测失败不应改变主流程结果。
        logger.warning("event sink failed: %s", type(exc).__name__)


async def check_cancelled(config: RunnableConfig | None) -> None:
    """输入：config；输出：无；作用：节点边界响应取消；实现：调用可选的 cancel_check。"""
    from src.graph.state import RunCancelled

    check = (config or {}).get("configurable", {}).get("cancel_check")
    if check and await _maybe_await(check()):
        raise RunCancelled("run cancelled")


def required_missing(brief: Brief | None) -> list[str]:
    """输入：Brief；输出：业务必填但缺失的字段；作用：路由 ask_user。"""
    if brief is None:
        return ["topic", *_REQUIRED_FIELDS]
    missing: list[str] = []
    if not brief.topic:
        missing.append("topic")
    for field in _REQUIRED_FIELDS:
        if getattr(brief, field) in (None, ""):
            missing.append(field)
    return missing


def apply_defaults(brief: Brief, risks: list[dict[str, Any]]) -> tuple[Brief, list[dict[str, Any]]]:
    """输入：Brief 与已有风险；输出：补默认值后的 Brief 和新风险。

    作用：允许 audience/style 缺省，但必须在结果中显式记录。
    实现：只补这两个非必填字段，重复执行幂等。
    """
    updates: dict[str, Any] = {}
    new_risks = list(risks)
    existing = {(risk.get("code"), risk.get("field")) for risk in new_risks}
    if not brief.audience:
        updates["audience"] = DEFAULT_AUDIENCE
        if ("DEFAULTED_FIELD", "audience") not in existing:
            new_risks.append(
                {"code": "DEFAULTED_FIELD", "field": "audience", "value": DEFAULT_AUDIENCE}
            )
    if not brief.style:
        updates["style"] = DEFAULT_STYLE
        if ("DEFAULTED_FIELD", "style") not in existing:
            new_risks.append(
                {"code": "DEFAULTED_FIELD", "field": "style", "value": DEFAULT_STYLE}
            )
    return (brief.model_copy(update=updates) if updates else brief), new_risks


def merge_materials(existing: list[Material], incoming: list[Material]) -> list[Material]:
    """输入：已有/新素材；输出：按 ID 去重的合并列表；作用：保持多次检索结果。"""
    merged = {material.id: material for material in existing}
    order = [material.id for material in existing]
    for material in incoming:
        if material.id not in merged:
            order.append(material.id)
        merged[material.id] = material
    return [merged[item] for item in order]


def script_summary(script: Script | None) -> dict[str, Any] | None:
    """输入：脚本；输出：给模型/Trace 的短摘要；作用：控制上下文大小；实现：只保留关键字段。"""
    if script is None:
        return None
    return {
        "script_id": script.script_id,
        "title": script.title,
        "shot_count": len(script.shots),
        "total_declared_sec": sum(shot.duration_sec for shot in script.shots),
        "shots": [
            {
                "shot_id": shot.shot_id,
                "narration": shot.narration,
                "subtitle": shot.subtitle,
                "duration_sec": shot.duration_sec,
                "claim_ids": shot.claim_ids,
            }
            for shot in script.shots
        ],
    }


def approved_claims(state: AgentState) -> list[Any]:
    """输入：状态；输出：approved 声明；作用：Writer/路由共用事实白名单。"""
    return [claim for claim in state.get("claims", []) if claim.approved]


def plain_brief(brief: Brief) -> Brief:
    """输入：Brief；输出：独立副本；作用：避免节点原地修改状态对象；实现：深拷贝。"""
    return brief.model_copy(deep=True)
