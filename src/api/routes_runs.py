"""运行相关 API。"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from src.api.deps import get_container, require_manager
from src.api.schemas import (
    CancelResponse,
    ResumeRequest,
    ResumeResponse,
    RunCreateRequest,
    RunCreateResponse,
    RunLinks,
    RunStatusResponse,
    TraceResponse,
)
from src.api.sse import parse_last_event_id, sse_response
from src.domain.status import TERMINAL_STATUSES, TaskStatus
from src.observability.trace import build_trace
from src.runtime.container import AppContainer

router = APIRouter(prefix="/v1/runs", tags=["runs"])


@router.post("", status_code=202, response_model=RunCreateResponse)
async def create_run(
    payload: RunCreateRequest,
    container: AppContainer = Depends(get_container),
) -> RunCreateResponse:
    """创建运行。

    输入：Brief。
    输出：202、run_id 与链接。
    作用：立即返回，后台运行图。
    实现：写 QUEUED → 启动 RunManager 任务。
    """
    manager = require_manager(container)
    run_id = str(uuid.uuid4())
    await container.run_store.create(
        run_id,
        request={"brief": payload.brief.model_dump(mode="json", exclude_none=True)},
        model="mock" if container.test_mode else container.settings.deepseek_model,
    )
    manager.start(run_id, payload.brief)
    return RunCreateResponse(
        run_id=run_id,
        status=TaskStatus.QUEUED.value,
        links=RunLinks(
            self=f"/v1/runs/{run_id}",
            events=f"/v1/runs/{run_id}/events",
            result=f"/v1/runs/{run_id}/result",
        ),
    )


@router.get("/{run_id}", response_model=RunStatusResponse)
async def get_run(
    run_id: str,
    container: AppContainer = Depends(get_container),
) -> RunStatusResponse:
    """查询运行状态。

    输入：run_id。
    输出：状态、当前节点、步数、缺失字段与错误。
    作用：轮询入口；等待输入时给出 missing_fields。
    实现：读 runs + 事件流中的 step/waiting 信息。
    """
    record = await container.run_store.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="run 不存在")
    events = await container.event_store.list_after(run_id, 0, limit=2000)
    step = 0
    missing: list[str] = []
    for event in events:
        payload = event.payload or {}
        if event.event_type in {"node.started", "action.selected"}:
            step = max(step, int(payload.get("step", 0) or 0))
        if event.event_type == "run.waiting_input":
            missing = [str(item) for item in payload.get("missing", [])]
    return RunStatusResponse(
        run_id=record.run_id,
        status=record.status,
        current_node=record.current_node,
        step=step,
        missing_fields=missing,
        error=record.error,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


@router.get("/{run_id}/result")
async def get_result(
    run_id: str,
    container: AppContainer = Depends(get_container),
) -> dict:
    """获取最终结果。

    输入：run_id。
    输出：SUCCESS/失败结果 JSON；未结束返回 409。
    作用：客户端拿到 sources/时长/risks/违规。
    实现：直接返回 runs.result，不重新计算。
    """
    record = await container.run_store.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="run 不存在")
    if record.result is None:
        raise HTTPException(
            status_code=409,
            detail={"message": "运行尚未产生最终结果", "status": record.status},
        )
    return record.result


@router.get("/{run_id}/events")
async def get_events(
    run_id: str,
    request: Request,
    last_event_id_header: str | None = Header(default=None, alias="Last-Event-ID"),
    last_event_id: int | None = Query(default=None),
    container: AppContainer = Depends(get_container),
):
    """SSE 事件流。

    输入：run_id、Last-Event-ID 或 last_event_id 查询参数。
    输出：text/event-stream。
    作用：实时观察运行；断线从 seq+1 恢复。
    实现：委托 sse 模块。
    """
    after = parse_last_event_id(last_event_id_header, last_event_id)
    return await sse_response(request, container, run_id, after)


@router.get("/{run_id}/trace", response_model=TraceResponse)
async def get_trace(
    run_id: str,
    format: Literal["json", "jsonl"] = Query(default="json"),
    container: AppContainer = Depends(get_container),
):
    """导出 Trace。

    输入：run_id 与格式。
    输出：JSON 或 JSONL。
    作用：复现动作、观察和决策依据。
    实现：读取事件并投影；jsonl 每行一个 trace 记录。
    """
    record = await container.run_store.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="run 不存在")
    events = await container.event_store.list_after(run_id, 0, limit=5000)
    trace = build_trace(run_id, events)
    if format == "jsonl":
        import json

        body = "\n".join(json.dumps(item, ensure_ascii=False) for item in trace)
        return PlainTextResponse(body + ("\n" if body else ""), media_type="application/x-ndjson")
    return TraceResponse(run_id=run_id, trace=trace)


@router.post("/{run_id}/resume", status_code=202, response_model=ResumeResponse)
async def resume_run(
    run_id: str,
    payload: ResumeRequest,
    container: AppContainer = Depends(get_container),
) -> ResumeResponse:
    """补充信息并恢复运行。

    输入：run_id 与答案。
    输出：202。
    作用：仅允许 WAITING_INPUT，防止重复恢复。
    实现：校验状态 → 标记 RUNNING → `Command(resume=answer)`。
    """
    manager = require_manager(container)
    record = await container.run_store.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="run 不存在")
    if record.status != TaskStatus.WAITING_INPUT.value:
        raise HTTPException(
            status_code=409,
            detail={"message": "只有 WAITING_INPUT 状态可恢复", "status": record.status},
        )
    await container.run_store.update(run_id, status=TaskStatus.RUNNING.value)
    manager.resume(run_id, payload.answer)
    return ResumeResponse(run_id=run_id, status=TaskStatus.RUNNING.value)


@router.post("/{run_id}/cancel", status_code=202, response_model=CancelResponse)
async def cancel_run(
    run_id: str,
    container: AppContainer = Depends(get_container),
) -> CancelResponse:
    """取消运行。

    输入：run_id。
    输出：202。
    作用：运行中在节点边界停止；等待输入时立即 CANCELLED。
    实现：终态返回 409，其余交给 RunManager。
    """
    manager = require_manager(container)
    record = await container.run_store.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="run 不存在")
    if record.status in {str(item) for item in TERMINAL_STATUSES}:
        raise HTTPException(status_code=409, detail={"message": "运行已结束", "status": record.status})
    accepted = await manager.cancel(run_id)
    if not accepted:
        raise HTTPException(status_code=409, detail="运行无法取消")
    return CancelResponse(run_id=run_id, status=TaskStatus.CANCELLED.value)
