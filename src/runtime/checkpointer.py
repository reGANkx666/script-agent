"""LangGraph checkpointer 工厂。

输入：配置。
输出：AsyncPostgresSaver 或 MemorySaver。
作用：生产运行可暂停/恢复；测试使用内存 checkpointer。
实现：显式 serde 白名单，避免未来严格 msgpack 破坏领域对象反序列化。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from src.config import Settings

_DOMAIN_CLASSES = [
    "Brief",
    "Material",
    "Claim",
    "Shot",
    "Script",
    "DurationReport",
    "Violation",
    "CheckReport",
    "ActionDecision",
]


def make_checkpoint_serde() -> JsonPlusSerializer:
    """输出：允许领域模型的序列化器；作用：稳定 checkpoint 反序列化。

    实现：仅放行本项目领域类，不使用全局 pickle/任意模块。
    """
    return JsonPlusSerializer(
        allowed_msgpack_modules=[("src.domain.models", name) for name in _DOMAIN_CLASSES]
    )


def make_memory_saver() -> MemorySaver:
    """输出：内存 checkpointer；作用：测试和场景 API；实现：注入领域 serde。"""
    return MemorySaver(serde=make_checkpoint_serde())


@asynccontextmanager
async def open_postgres_checkpointer(settings: Settings) -> AsyncIterator[AsyncPostgresSaver]:
    """打开并初始化 PostgreSQL checkpointer。

    输入：配置。
    输出：异步上下文中的 `AsyncPostgresSaver`。
    作用：FastAPI lifespan 内保持长连接，避免每请求 setup。
    实现：`from_conn_string` + `setup()`；失败向上抛，由就绪状态处理。
    """
    async with AsyncPostgresSaver.from_conn_string(
        settings.database_url, serde=make_checkpoint_serde()
    ) as checkpointer:
        await checkpointer.setup()
        yield checkpointer
