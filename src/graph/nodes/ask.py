"""用户澄清节点。"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from src.domain.models import Brief
from src.graph.nodes.common import apply_defaults, emit, required_missing
from src.graph.services import GraphServices
from src.graph.state import AgentState

_FIELD_LABELS = {
    "platform": "发布平台（例如 抖音）",
    "target_duration_sec": "目标时长（秒）",
    "topic": "视频主题",
}
_ALLOWED_ANSWER_FIELDS = {"platform", "target_duration_sec", "audience", "style"}


def make_ask_node(services: GraphServices):
    """输入：依赖容器；输出：ask_user 节点。"""

    async def ask_user(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        """暂停并等待用户补充信息。

        输入：缺失字段；恢复时接收答案字典。
        输出：合并后的 Brief、新 missing_fields。
        作用：不猜测业务必填值；恢复后重新进入 Agent。
        实现：`interrupt()` 前无数据库副作用；恢复值只接受 Brief 白名单字段。
        """
        missing = list(state.get("missing_fields", [])) or required_missing(state.get("brief"))
        action = state.get("pending_action")
        args = action.args if action else {}
        question = str(args.get("question") or _make_question(missing))
        payload = {
            "type": "NEEDS_INPUT",
            "run_id": state.get("run_id"),
            "missing": missing,
            "question": question,
        }
        answer = interrupt(payload)
        normalized = _normalize_answer(answer)
        brief = state.get("brief")
        risks = list(state.get("risks", []))
        if brief is not None and normalized:
            merged = {**brief.model_dump(), **normalized}
            try:
                brief = Brief.model_validate(merged)
                brief, risks = apply_defaults(brief, risks)
            except Exception:
                # 无效补充值不被接受，继续保留缺失字段并再次询问。
                brief = state.get("brief")
        remaining = required_missing(brief)
        await emit(
            config,
            "tool.result",
            "ask_user",
            {"resumed": True, "missing_fields": remaining},
        )
        return {
            "brief": brief,
            "risks": risks,
            "missing_fields": remaining,
            "pending_question": None,
            "pending_action": None,
            "status": "RUNNING",
            "last_observation": {
                "action": "ask_user",
                "status": "RESUMED",
                "missing_fields": remaining,
            },
        }

    return ask_user


def _make_question(missing: list[str]) -> str:
    """输入：缺失字段；输出：中文问题；作用：让等待输入可读；实现：字段标签映射。"""
    labels = [_FIELD_LABELS.get(field, field) for field in missing]
    return "请补充以下信息后才能继续：" + "、".join(labels)


def _normalize_answer(answer: Any) -> dict[str, Any]:
    """输入：resume 值；输出：可合并到 Brief 的字段；作用：隔离 API 外层结构。

    实现：支持直接字典与 `{"answer": {...}}`；忽略未知字段；目标时长转 int。
    """
    if not isinstance(answer, dict):
        return {}
    nested = answer.get("answer")
    if isinstance(nested, dict):
        answer = nested
    result: dict[str, Any] = {}
    for field in _ALLOWED_ANSWER_FIELDS:
        if field not in answer or answer[field] in (None, ""):
            continue
        value = answer[field]
        if field == "target_duration_sec":
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
        else:
            value = str(value).strip()
            if not value:
                continue
        result[field] = value
    return result
