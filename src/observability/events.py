"""事件类型常量。

输入：节点/运行管理器事件。
输出：统一字符串常量。
作用：避免 SSE、存储、测试中出现拼写漂移。
实现：StrEnum；序列化即字符串。
"""

from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    """允许写入 events 表的事件类型。"""

    NODE_STARTED = "node.started"
    NODE_COMPLETED = "node.completed"
    NODE_FAILED = "node.failed"
    ACTION_SELECTED = "action.selected"
    TOOL_CALLED = "tool.called"
    TOOL_RESULT = "tool.result"
    RUN_WAITING_INPUT = "run.waiting_input"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"


ALLOWED_EVENT_TYPES: frozenset[str] = frozenset(str(item) for item in EventType)
