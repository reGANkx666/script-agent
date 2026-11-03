"""领域枚举。

输入：运行状态、动作名和失败状态常量。
输出：可由 Pydantic/FastAPI 直接序列化的字符串枚举。
作用：保持 API、图节点和数据库中的状态值唯一，避免字符串拼写漂移。
实现：使用 `StrEnum`，序列化后即普通字符串。
"""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    """运行生命周期状态。"""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    SUCCESS = "SUCCESS"
    NEEDS_INPUT = "NEEDS_INPUT"
    NO_MATERIAL = "NO_MATERIAL"
    TOOL_ERROR = "TOOL_ERROR"
    UNSATISFIABLE_DURATION = "UNSATISFIABLE_DURATION"
    UNSATISFIABLE_RULES = "UNSATISFIABLE_RULES"
    MAX_STEPS = "MAX_STEPS"
    MODEL_ERROR = "MODEL_ERROR"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.SUCCESS,
        TaskStatus.NEEDS_INPUT,
        TaskStatus.NO_MATERIAL,
        TaskStatus.TOOL_ERROR,
        TaskStatus.UNSATISFIABLE_DURATION,
        TaskStatus.UNSATISFIABLE_RULES,
        TaskStatus.MAX_STEPS,
        TaskStatus.MODEL_ERROR,
        TaskStatus.CANCELLED,
        TaskStatus.INTERRUPTED,
    }
)

FAILURE_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.NEEDS_INPUT,
        TaskStatus.NO_MATERIAL,
        TaskStatus.TOOL_ERROR,
        TaskStatus.UNSATISFIABLE_DURATION,
        TaskStatus.UNSATISFIABLE_RULES,
        TaskStatus.MAX_STEPS,
        TaskStatus.MODEL_ERROR,
        TaskStatus.CANCELLED,
        TaskStatus.INTERRUPTED,
    }
)

# `fail` 动作允许模型显式选择的失败状态；MAX_STEPS 由编排器强制产生。
MODEL_SELECTABLE_FAILURES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.NEEDS_INPUT,
        TaskStatus.NO_MATERIAL,
        TaskStatus.TOOL_ERROR,
        TaskStatus.UNSATISFIABLE_DURATION,
        TaskStatus.UNSATISFIABLE_RULES,
        TaskStatus.MODEL_ERROR,
    }
)


class ActionName(StrEnum):
    """Agent 节点的可执行动作。"""

    SEARCH_MATERIALS = "search_materials"
    DRAFT_SCRIPT = "draft_script"
    ESTIMATE_DURATION = "estimate_duration"
    CHECK_RULES = "check_rules"
    REVISE_SCRIPT = "revise_script"
    ASK_USER = "ask_user"
    FINALIZE = "finalize"
    FAIL = "fail"
