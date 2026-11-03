"""Trace 组装。

输入：事件记录。
输出：可导出的 JSON Trace 行。
作用：把运行事件投影成“步骤、动作、观察、依据”的可解释记录。
实现：只使用已持久化事件，不读取模型隐藏推理或密钥。
"""

from __future__ import annotations

from typing import Any

from src.runtime.store_types import EventRecord


def build_trace(run_id: str, events: list[EventRecord]) -> list[dict[str, Any]]:
    """构建 Trace 行。

    输入：run_id 与按 seq 排序的事件。
    输出：Trace 字典列表。
    作用：GET /trace 与 examples/traces 共用。
    实现：每个事件保留 seq/node/type；action/tool 事件补充动作和观察字段。
    """
    trace: list[dict[str, Any]] = []
    current_step = 0
    last_action: str | None = None
    last_action_time = None
    for index, event in enumerate(events):
        payload = event.payload or {}
        if event.event_type == "action.selected":
            current_step = int(payload.get("step", current_step))
            last_action = str(payload.get("action", "")) or None
            last_action_time = event.created_at
            trace.append(
                {
                    "run_id": run_id,
                    "seq": event.seq,
                    "step": current_step,
                    "node": event.node,
                    "event_type": event.event_type,
                    "action": last_action,
                    "args": payload.get("args", {}),
                    "basis": payload.get("basis", ""),
                    "observation": None,
                    "next_action": _next_action(events, index),
                    "latency_ms": None,
                }
            )
            continue
        next_action = _next_action(events, index)
        latency_ms = None
        if last_action_time is not None and event.created_at is not None:
            try:
                latency_ms = int((event.created_at - last_action_time).total_seconds() * 1000)
            except (TypeError, ValueError):
                latency_ms = None
        trace.append(
            {
                "run_id": run_id,
                "seq": event.seq,
                "step": current_step,
                "node": event.node,
                "event_type": event.event_type,
                "action": last_action,
                "args": {},
                "basis": "",
                "observation": _sanitize_payload(payload),
                "next_action": next_action,
                "latency_ms": latency_ms,
            }
        )
    return trace


def _next_action(events: list[EventRecord], start_index: int) -> str | None:
    """输入：事件列表与当前位置；输出：下一条 action.selected 的动作；作用：Trace 可读性。"""
    for event in events[start_index + 1 :]:
        if event.event_type == "action.selected":
            return str((event.payload or {}).get("action") or "") or None
    return None


def _sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """输入：事件载荷；输出：可安全导出的字段；作用：过滤潜在 secret/长文本。

    实现：递归把疑似 secret 字段替换为 `[REDACTED]`，其余保留短值。
    """
    blocked = {"api_key", "authorization", "password", "token", "secret"}
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if any(part in key.lower() for part in blocked):
            result[key] = "[REDACTED]"
        elif isinstance(value, dict):
            result[key] = _sanitize_payload(value)
        elif isinstance(value, str) and len(value) > 2000:
            result[key] = value[:2000]
        else:
            result[key] = value
    return result
