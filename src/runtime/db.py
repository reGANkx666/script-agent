"""PostgreSQL 连接与就绪检查。

输入：`DATABASE_URL`。
输出：连接池和 readiness 结果。
作用：集中管理连接生命周期，避免各仓库自行建池。
实现：psycopg AsyncConnectionPool；只在当前库内检查，不创建数据库。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from psycopg_pool import AsyncConnectionPool

from src.config import Settings


@dataclass
class ReadinessState:
    """就绪状态。"""

    ready: bool = False
    errors: list[str] = field(default_factory=list)
    details: dict[str, str] = field(default_factory=dict)


async def create_pool(settings: Settings) -> AsyncConnectionPool:
    """创建并打开连接池。

    输入：配置。
    输出：已打开的连接池；失败抛异常。
    作用：业务存储与规则仓库共享连接。
    实现：`open(wait=True)` 在启动期显式验证连接。
    """
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL 未配置")
    pool = AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=1,
        max_size=5,
        open=False,
        timeout=settings.tool_timeout_seconds,
        kwargs={"autocommit": True},
    )
    try:
        await pool.open(wait=True, timeout=settings.tool_timeout_seconds)
    except Exception:
        await pool.close()
        raise
    return pool


async def check_database(pool: AsyncConnectionPool) -> ReadinessState:
    """检查数据库、pgvector、表和向量维度。

    输入：连接池。
    输出：`ReadinessState`；任一项失败都 ready=False。
    作用：给 `/readyz` 提供明确原因，不掩盖配置问题。
    实现：只读查询 pg_extension / to_regclass / pg_attribute。
    """
    state = ReadinessState()
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
                )
                row = await cur.fetchone()
                if not row:
                    state.errors.append("pgvector 扩展未启用")
                else:
                    state.details["pgvector"] = str(row[0])
                for table in (
                    "materials",
                    "brand_rules",
                    "platform_rules",
                    "runs",
                    "events",
                    "knowledge_documents",
                    "knowledge_chunks",
                    "knowledge_ingestion_jobs",
                ):
                    await cur.execute("SELECT to_regclass(%s) AS name", (f"public.{table}",))
                    exists = await cur.fetchone()
                    if not exists or not exists[0]:
                        state.errors.append(f"缺少表 {table}")
                await cur.execute(
                    """
                    SELECT format_type(a.atttypid, a.atttypmod) AS column_type
                    FROM pg_attribute a
                    JOIN pg_class c ON c.oid = a.attrelid
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relname = 'materials'
                      AND a.attname = 'embedding' AND a.attnum > 0
                    """
                )
                column = await cur.fetchone()
                column_type = str(column[0]) if column else ""
                state.details["embedding_column"] = column_type
                if "vector(1024)" not in column_type:
                    state.errors.append(f"materials.embedding 必须是 vector(1024)，实际 {column_type or '缺失'}")
    except Exception as exc:
        state.errors.append(f"数据库检查失败：{type(exc).__name__}")
    state.ready = not state.errors
    return state


async def close_pool(pool: AsyncConnectionPool) -> None:
    """输入：连接池；输出：无；作用：优雅关闭；实现：pool.close。"""
    await pool.close()
