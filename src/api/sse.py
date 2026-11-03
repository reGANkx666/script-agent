"""SSE 事件流。

输入：run_id、Last-Event-ID。
输出：text/event-stream 事件。
作用：实时推送节点、动作、工具和终态事件，支持断线续传。
实现：Postgres/内存事件存储轮询；15 秒心跳；终态且追平后关闭。
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from src.domain.status import TERMINAL_STATUSES
from src.runtime.container import AppContainer


async def sse_response(
    request: Request,
    container: AppContainer,
    run_id: str,
    last_event_id: int = 0,
) -> StreamingResponse:
    """构造 SSE 响应。

    输入：请求、容器、run_id、重连位置。
    输出：`StreamingResponse`。
    作用：GET /v1/runs/{id}/events。
    实现：生成器增量读取事件；按 seq 设置 SSE id；心跳保持连接。
    """
    record = await container.run_store.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="run 不存在")

    async def generator() -> AsyncIterator[str]:
        """输入：无；输出：SSE 文本块；作用：流式推送；实现：轮询+心跳+断线检测。"""
        after = max(0, last_event_id)
        terminal = {str(item) for item in TERMINAL_STATUSES}
        idle_seconds = 0.0
        while True:
            if await request.is_disconnected():
                break
            events = await container.event_store.list_after(run_id, after, limit=100)
            for event in events:
                after = event.seq
                data = {
                    "run_id": run_id,
                    "seq": event.seq,
                    "node": event.node,
                    "event_type": event.event_type,
                    **event.payload,
                }
                yield f"id: {event.seq}\nevent: {event.event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                idle_seconds = 0.0
            current = await container.run_store.get(run_id)
            if current is None or current.status in terminal:
                # 终态后再读一次，防止状态落库与最后一条事件之间存在窗口。
                final_events = await container.event_store.list_after(run_id, after, limit=100)
                if not final_events:
                    break
                continue
            await asyncio.sleep(0.5)
            idle_seconds += 0.5
            if idle_seconds >= 15:
                idle_seconds = 0.0
                yield ": ping\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def parse_last_event_id(header_value: str | None, query_value: int | None) -> int:
    """输入：Header 与 query；输出：起始 seq；作用：兼容浏览器重连与显式参数。

    实现：query 优先；非法值回退 0。
    """
    if query_value is not None:
        return max(0, query_value)
    if not header_value:
        return 0
    try:
        return max(0, int(header_value))
    except ValueError:
        return 0
