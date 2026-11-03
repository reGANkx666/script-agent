"""运行状态存储。

输入：RunManager 的状态更新。
输出：PostgreSQL 或内存中的 `runs` 记录。
作用：支撑 GET /runs、恢复、取消和启动恢复。
实现：Protocol + psycopg 原生 SQL + MemoryRunStore；不引入 ORM。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from src.runtime.store_types import RunRecord


class RunStore(Protocol):
    """运行存储接口。"""

    async def create(self, run_id: str, request: dict[str, Any], model: str) -> RunRecord:
        """输入：run_id、请求、模型；输出：QUEUED 记录；作用：创建运行。"""
        ...

    async def get(self, run_id: str) -> RunRecord | None:
        """输入：run_id；输出：记录或 None；作用：状态查询。"""
        ...

    async def update(
        self,
        run_id: str,
        *,
        status: str | None = None,
        current_node: str | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """输入：可选状态/节点/结果；输出：无；作用：增量更新运行。"""
        ...

    async def mark_stale_running(self) -> int:
        """输出：被标记数量；作用：进程重启后把遗留 RUNNING 改成 INTERRUPTED。"""
        ...


class PostgresRunStore:
    """PostgreSQL 运行存储。"""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """输入：连接池；输出：存储；作用：复用业务连接；实现：原生 SQL。"""
        self.pool = pool

    async def create(self, run_id: str, request: dict[str, Any], model: str) -> RunRecord:
        """输入：run_id/请求/模型；输出：新记录；作用：创建 QUEUED 行。"""
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO runs (run_id, status, request, model)
                VALUES (%s, 'QUEUED', %s, %s)
                """,
                (run_id, Jsonb(request), model),
            )
        record = await self.get(run_id)
        assert record is not None
        return record

    async def get(self, run_id: str) -> RunRecord | None:
        """输入：run_id；输出：记录；作用：查询状态；实现：单行 SELECT。"""
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT run_id::text, status, request, result, error,
                           current_node, model, created_at, updated_at
                    FROM runs WHERE run_id = %s
                    """,
                    (run_id,),
                )
                row = await cur.fetchone()
        return _row_to_run(row) if row else None

    async def update(
        self,
        run_id: str,
        *,
        status: str | None = None,
        current_node: str | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """输入：可选字段；输出：无；作用：动态更新运行；实现：白名单列拼接 SQL。"""
        clauses = ["updated_at = now()"]
        params: list[Any] = []
        if status is not None:
            clauses.append("status = %s")
            params.append(str(status))
        if current_node is not None:
            clauses.append("current_node = %s")
            params.append(current_node)
        if result is not None:
            clauses.append("result = %s")
            params.append(Jsonb(result))
        if error is not None:
            clauses.append("error = %s")
            params.append(Jsonb(error))
        params.append(run_id)
        async with self.pool.connection() as conn:
            await conn.execute(
                f"UPDATE runs SET {', '.join(clauses)} WHERE run_id = %s",
                params,
            )

    async def mark_stale_running(self) -> int:
        """输出：数量；作用：启动恢复；实现：UPDATE RUNNING→INTERRUPTED。"""
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE runs SET status='INTERRUPTED', updated_at=now() WHERE status='RUNNING'"
                )
                return cur.rowcount or 0


class MemoryRunStore:
    """内存运行存储，用于测试和同步场景运行。"""

    def __init__(self) -> None:
        """输入：无；输出：存储；作用：不依赖 Postgres 跑 API 测试。"""
        self._runs: dict[str, RunRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, run_id: str, request: dict[str, Any], model: str) -> RunRecord:
        """输入：run_id/请求/模型；输出：记录；作用：内存创建。"""
        now = datetime.now(UTC)
        record = RunRecord(
            run_id=run_id,
            status="QUEUED",
            request=dict(request),
            model=model,
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            self._runs[run_id] = record
        return record

    async def get(self, run_id: str) -> RunRecord | None:
        """输入：run_id；输出：独立副本；作用：避免调用方修改内部状态。"""
        async with self._lock:
            record = self._runs.get(run_id)
            return _copy_run(record) if record else None

    async def update(
        self,
        run_id: str,
        *,
        status: str | None = None,
        current_node: str | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """输入：可选字段；输出：无；作用：内存增量更新；实现：加锁替换。"""
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return
            if status is not None:
                record.status = str(status)
            if current_node is not None:
                record.current_node = current_node
            if result is not None:
                record.result = result
            if error is not None:
                record.error = error
            record.updated_at = datetime.now(UTC)

    async def mark_stale_running(self) -> int:
        """输出：数量；作用：内存实现启动恢复。"""
        count = 0
        async with self._lock:
            for record in self._runs.values():
                if record.status == "RUNNING":
                    record.status = "INTERRUPTED"
                    count += 1
        return count


def _row_to_run(row: tuple[Any, ...]) -> RunRecord:
    """输入：数据库行；输出：RunRecord；作用：集中字段映射。"""
    return RunRecord(
        run_id=row[0],
        status=row[1],
        request=row[2] or {},
        result=row[3],
        error=row[4],
        current_node=row[5],
        model=row[6],
        created_at=row[7],
        updated_at=row[8],
    )


def _copy_run(record: RunRecord) -> RunRecord:
    """输入：记录；输出：浅拷贝；作用：测试隔离；实现：dict/list 复制。"""
    return RunRecord(
        run_id=record.run_id,
        status=record.status,
        request=dict(record.request),
        result=dict(record.result) if record.result else None,
        error=dict(record.error) if record.error else None,
        current_node=record.current_node,
        model=record.model,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )
