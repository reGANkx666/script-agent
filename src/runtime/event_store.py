"""事件存储。

输入：节点/运行事件。
输出：可按 seq 回放的事件流。
作用：SSE 断线重连、Trace 导出和运行诊断。
实现：PostgreSQL 使用 `(run_id, seq)` 唯一约束；内存实现用于测试。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from src.runtime.store_types import EventRecord


class EventStore(Protocol):
    """事件存储接口。"""

    async def append(
        self,
        run_id: str,
        event_type: str,
        node: str | None,
        payload: dict[str, Any],
    ) -> EventRecord:
        """输入：run_id/类型/节点/载荷；输出：持久化事件；作用：统一事件序列。"""
        ...

    async def list_after(
        self, run_id: str, after_seq: int = 0, limit: int = 500
    ) -> list[EventRecord]:
        """输入：run_id 与起始 seq；输出：事件列表；作用：SSE 增量读取。"""
        ...

    async def max_seq(self, run_id: str) -> int:
        """输入：run_id；输出：最大 seq；作用：SSE 终点判断。"""
        ...


class PostgresEventStore:
    """PostgreSQL 事件存储。"""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """输入：连接池；输出：存储；作用：持久化事件；实现：原生 SQL。"""
        self.pool = pool

    async def append(
        self,
        run_id: str,
        event_type: str,
        node: str | None,
        payload: dict[str, Any],
    ) -> EventRecord:
        """输入：事件字段；输出：新事件；作用：写入事件表。

        实现：同一 run 由 RunManager 串行写入，`MAX(seq)+1` 足够；唯一约束兜底。
        """
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO events (run_id, seq, event_type, node, payload)
                    VALUES (
                      %s,
                      COALESCE((SELECT MAX(seq) + 1 FROM events WHERE run_id = %s), 1),
                      %s, %s, %s
                    )
                    RETURNING id, seq, created_at
                    """,
                    (run_id, run_id, event_type, node, Jsonb(payload)),
                )
                row = await cur.fetchone()
        return EventRecord(
            id=row[0],
            run_id=run_id,
            seq=row[1],
            event_type=event_type,
            node=node,
            payload=payload,
            created_at=row[2],
        )

    async def list_after(
        self, run_id: str, after_seq: int = 0, limit: int = 500
    ) -> list[EventRecord]:
        """输入：run_id/after_seq/limit；输出：按 seq 升序事件；作用：SSE 重放。"""
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, run_id::text, seq, event_type, node, payload, created_at
                    FROM events
                    WHERE run_id = %s AND seq > %s
                    ORDER BY seq ASC
                    LIMIT %s
                    """,
                    (run_id, after_seq, limit),
                )
                rows = await cur.fetchall()
        return [
            EventRecord(
                id=row[0],
                run_id=row[1],
                seq=row[2],
                event_type=row[3],
                node=row[4],
                payload=row[5] or {},
                created_at=row[6],
            )
            for row in rows
        ]

    async def max_seq(self, run_id: str) -> int:
        """输入：run_id；输出：最大 seq；作用：SSE 判断是否已追平。"""
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id = %s", (run_id,))
                row = await cur.fetchone()
        return int(row[0]) if row else 0


class MemoryEventStore:
    """内存事件存储。"""

    def __init__(self) -> None:
        """输入：无；输出：存储；作用：测试 SSE/场景。"""
        self._events: dict[str, list[EventRecord]] = {}
        self._next_id = 1
        self._lock = asyncio.Lock()

    async def append(
        self,
        run_id: str,
        event_type: str,
        node: str | None,
        payload: dict[str, Any],
    ) -> EventRecord:
        """输入：事件字段；输出：事件；作用：内存追加；实现：加锁维护 seq。"""
        async with self._lock:
            bucket = self._events.setdefault(run_id, [])
            seq = (bucket[-1].seq + 1) if bucket else 1
            record = EventRecord(
                id=self._next_id,
                run_id=run_id,
                seq=seq,
                event_type=event_type,
                node=node,
                payload=dict(payload),
                created_at=datetime.now(UTC),
            )
            self._next_id += 1
            bucket.append(record)
            return record

    async def list_after(
        self, run_id: str, after_seq: int = 0, limit: int = 500
    ) -> list[EventRecord]:
        """输入：run_id/after_seq/limit；输出：事件副本；作用：SSE 增量读取。"""
        async with self._lock:
            return [
                EventRecord(
                    id=item.id,
                    run_id=item.run_id,
                    seq=item.seq,
                    event_type=item.event_type,
                    node=item.node,
                    payload=dict(item.payload),
                    created_at=item.created_at,
                )
                for item in self._events.get(run_id, [])
                if item.seq > after_seq
            ][:limit]

    async def max_seq(self, run_id: str) -> int:
        """输入：run_id；输出：最大 seq。"""
        async with self._lock:
            bucket = self._events.get(run_id, [])
            return bucket[-1].seq if bucket else 0
