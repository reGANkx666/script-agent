"""运行管理器。

输入：已创建的 run_id、Brief 或恢复答案。
输出：LangGraph 执行、状态/事件持久化和终态。
作用：把图的执行与 FastAPI 请求生命周期解耦。
实现：每个 run 一个 asyncio.Lock；事件 sink 写事件表；中断时记录 WAITING_INPUT。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langgraph.types import Command

from src.config import Settings
from src.domain.models import Brief
from src.domain.status import TERMINAL_STATUSES, TaskStatus
from src.graph.state import RunCancelled, initial_state
from src.observability.events import EventType
from src.runtime.assembler import exception_result, interrupt_payloads
from src.runtime.event_store import EventStore
from src.runtime.run_store import RunStore

logger = logging.getLogger(__name__)


class RunManager:
    """后台运行编排器。"""

    def __init__(
        self,
        settings: Settings,
        graph: Any,
        run_store: RunStore,
        event_store: EventStore,
    ) -> None:
        """输入：配置、编译图、存储；输出：管理器；作用：管理任务与并发。"""
        self.settings = settings
        self.graph = graph
        self.run_store = run_store
        self.event_store = event_store
        self._tasks: set[asyncio.Task] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._cancelled: set[str] = set()
        self._active: set[str] = set()

    async def recover_stale_runs(self) -> int:
        """输出：恢复数量；作用：启动时清零遗留 RUNNING；实现：委托存储。"""
        return await self.run_store.mark_stale_running()

    def start(self, run_id: str, brief: Brief) -> None:
        """启动一次新运行。

        输入：run_id 与 Brief。
        输出：无；后台任务继续执行。
        作用：API 立即返回 202；客户端轮询/SSE。
        实现：`asyncio.create_task`，任务集合防 GC。
        """
        task = asyncio.create_task(self._execute_fresh(run_id, brief), name=f"run:{run_id}")
        self._track(task)

    def resume(self, run_id: str, answer: dict[str, Any]) -> None:
        """恢复 WAITING_INPUT 运行。

        输入：run_id 与用户答案。
        输出：无；后台任务继续执行。
        作用：使用同一 thread_id 从 checkpoint 恢复，不重置已有素材。
        实现：`Command(resume=answer)`。
        """
        task = asyncio.create_task(self._execute_resume(run_id, answer), name=f"resume:{run_id}")
        self._track(task)

    async def cancel(self, run_id: str) -> bool:
        """请求取消运行。

        输入：run_id。
        输出：是否接受。
        作用：运行中在节点边界停止；等待输入时直接终态。
        实现：记录取消标记；若当前无活动任务则立即写 CANCELLED。
        """
        self._cancelled.add(run_id)
        if run_id in self._active:
            return True
        record = await self.run_store.get(run_id)
        if record is None:
            return False
        if record.status in {str(item) for item in TERMINAL_STATUSES}:
            return False
        await self.run_store.update(
            run_id,
            status=TaskStatus.CANCELLED.value,
            error={"status": TaskStatus.CANCELLED.value, "message": "运行已取消"},
        )
        await self._append_event(
            run_id,
            EventType.RUN_FAILED.value,
            None,
            {"status": TaskStatus.CANCELLED.value, "message": "运行已取消"},
        )
        return True

    async def _execute_fresh(self, run_id: str, brief: Brief) -> None:
        """执行新运行。

        输入：run_id/Brief；输出：无。
        作用：设置 RUNNING、构建初始状态并驱动图。
        实现：记录 active、加锁、调用 `_drive`。
        """
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            self._active.add(run_id)
            try:
                if run_id in self._cancelled:
                    await self._mark_cancelled(run_id)
                    return
                await self.run_store.update(run_id, status=TaskStatus.RUNNING.value)
                state = initial_state(run_id, brief)
                await self._drive(run_id, state)
            except RunCancelled:
                await self._mark_cancelled(run_id)
            except Exception as exc:
                await self._mark_exception(run_id, exc)
            finally:
                self._active.discard(run_id)
                self._locks.pop(run_id, None)

    async def _execute_resume(self, run_id: str, answer: dict[str, Any]) -> None:
        """执行恢复。

        输入：run_id/答案；输出：无；作用：从 checkpoint 继续图执行。
        实现：`Command(resume=answer)` 输入；其余同新运行。
        """
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            self._active.add(run_id)
            try:
                if run_id in self._cancelled:
                    await self._mark_cancelled(run_id)
                    return
                await self.run_store.update(run_id, status=TaskStatus.RUNNING.value)
                await self._drive(run_id, Command(resume=answer))
            except RunCancelled:
                await self._mark_cancelled(run_id)
            except Exception as exc:
                await self._mark_exception(run_id, exc)
            finally:
                self._active.discard(run_id)
                self._locks.pop(run_id, None)

    async def _drive(self, run_id: str, graph_input: Any) -> None:
        """驱动图到下一次暂停或终态。

        输入：run_id、图输入（状态或 Command）。
        输出：无。
        作用：消费 graph.astream，检测中断并持久化终态。
        实现：事件由 sink 增量落库；结束后读取快照判断 interrupt。
        """
        config = self._config(run_id)
        async for _update in self.graph.astream(graph_input, config, stream_mode="updates"):
            if run_id in self._cancelled:
                raise RunCancelled("run cancelled")
        snapshot = await self.graph.aget_state(config)
        waiting = interrupt_payloads(snapshot)
        if waiting:
            question = waiting[0]
            await self.run_store.update(
                run_id,
                status=TaskStatus.WAITING_INPUT.value,
                current_node="ask_user",
            )
            await self._append_event(
                run_id,
                EventType.RUN_WAITING_INPUT.value,
                "ask_user",
                question,
            )
            return

        values = snapshot.values or {}
        status = str(values.get("status") or TaskStatus.INTERRUPTED.value)
        if status not in {str(item) for item in TERMINAL_STATUSES}:
            status = TaskStatus.INTERRUPTED.value
        result = values.get("result")
        error = values.get("error")
        if status != TaskStatus.SUCCESS.value and error is None:
            error = {"status": status, "message": "运行结束但未返回成功结果"}
        await self.run_store.update(
            run_id,
            status=status,
            current_node=values.get("current_node"),
            result=result,
            error=error,
        )

    def _config(self, run_id: str) -> dict[str, Any]:
        """输入：run_id；输出：LangGraph config；作用：绑定 checkpoint 和观测。

        实现：thread_id=run_id；注入事件 sink 与取消检查；设置递归上限。
        """

        async def sink(event: dict[str, Any]) -> None:
            node = event.get("node")
            event_type = str(event.get("event_type", ""))
            await self._append_event(run_id, event_type, node, event.get("payload") or {})
            if event_type == EventType.NODE_STARTED.value and node:
                await self.run_store.update(run_id, current_node=node)

        async def cancel_check() -> bool:
            return run_id in self._cancelled

        return {
            "configurable": {
                "thread_id": run_id,
                "event_sink": sink,
                "cancel_check": cancel_check,
            },
            "recursion_limit": self.settings.recursion_limit,
        }

    async def _append_event(
        self, run_id: str, event_type: str, node: str | None, payload: dict[str, Any]
    ) -> None:
        """输入：事件字段；输出：无；作用：统一落库；实现：异常只记录日志。"""
        try:
            await self.event_store.append(run_id, event_type, node, payload)
        except Exception as exc:
            logger.warning("event append failed run=%s type=%s error=%s", run_id, event_type, type(exc).__name__)

    async def _mark_cancelled(self, run_id: str) -> None:
        """输入：run_id；输出：无；作用：记录 CANCELLED；实现：状态 + 事件。"""
        await self.run_store.update(
            run_id,
            status=TaskStatus.CANCELLED.value,
            error={"status": TaskStatus.CANCELLED.value, "message": "运行已取消"},
        )
        await self._append_event(
            run_id,
            EventType.RUN_FAILED.value,
            None,
            {"status": TaskStatus.CANCELLED.value, "message": "运行已取消"},
        )

    async def _mark_exception(self, run_id: str, exc: Exception) -> None:
        """输入：run_id/异常；输出：无；作用：未捕获异常显式终态；实现：不泄漏异常消息。"""
        status = TaskStatus.INTERRUPTED
        message = f"运行异常中断：{type(exc).__name__}"
        await self.run_store.update(
            run_id,
            status=status.value,
            result=exception_result(status.value, message),
            error={"status": status.value, "message": message},
        )
        await self._append_event(
            run_id,
            EventType.RUN_FAILED.value,
            None,
            {"status": status.value, "message": message},
        )

    def _track(self, task: asyncio.Task) -> None:
        """输入：后台任务；输出：无；作用：防 GC 并回收引用；实现：done callback。"""
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
