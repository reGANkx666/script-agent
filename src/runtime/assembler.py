"""运行结果与中断快照辅助。

输入：图快照、异常。
输出：终态错误/结果结构。
作用：让 RunManager 与测试运行器共享稳定的结果形状。
实现：只做投影，不重新执行业务判断。
"""

from __future__ import annotations

from typing import Any


def interrupt_payloads(snapshot: Any) -> list[dict[str, Any]]:
    """输入：LangGraph 状态快照；输出：interrupt 载荷列表；作用：识别 WAITING_INPUT。

    实现：遍历 task.interrupts，兼容不同 LangGraph 版本的对象结构。
    """
    payloads: list[dict[str, Any]] = []
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            value = getattr(item, "value", None)
            if isinstance(value, dict):
                payloads.append(value)
    return payloads


def exception_result(status: str, message: str) -> dict[str, Any]:
    """输入：状态和消息；输出：最小失败结果；作用：未捕获异常也能返回可读结构。"""
    return {
        "status": status,
        "script": None,
        "sources": [],
        "estimated_duration_sec": None,
        "target_duration_sec": None,
        "risks": [],
        "violations": [],
        "attempts": {},
        "suggestions": [],
        "error": {"status": status, "message": message},
    }


def terminal_summary(values: dict[str, Any]) -> dict[str, Any]:
    """输入：最终状态；输出：GET /runs 摘要；作用：控制响应体积；实现：只取关键字段。"""
    result = values.get("result") or {}
    return {
        "status": values.get("status"),
        "current_node": values.get("current_node"),
        "step": values.get("step", 0),
        "tool_calls": values.get("tool_calls", 0),
        "estimated_duration_sec": result.get("estimated_duration_sec"),
        "source_ids": [item.get("source_id") for item in result.get("sources", [])],
    }
