"""应用依赖容器。

输入：配置。
输出：已就绪的图、运行管理器、存储与就绪状态。
作用：FastAPI lifespan 只负责打开/关闭资源，路由只依赖容器。
实现：配置/数据库/embedding/checkpointer 分阶段构建；任一失败则显式不可运行。
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from src.config import Settings
from src.graph.services import GraphServices
from src.knowledge.ingestion import IngestionManager, KnowledgeIngestionService
from src.knowledge.repository import PostgresKnowledgeRepository
from src.llm.deepseek import DeepSeekDecisionModel, DeepSeekWriterModel
from src.runtime.checkpointer import open_postgres_checkpointer
from src.runtime.db import ReadinessState, check_database, close_pool, create_pool
from src.runtime.event_store import EventStore, PostgresEventStore
from src.runtime.graph_factory import build_runtime_graph
from src.runtime.repositories import PostgresMaterialRepository, PostgresRuleProvider
from src.runtime.run_manager import RunManager
from src.runtime.run_store import PostgresRunStore, RunStore
from src.tools.embeddings import DashScopeEmbedder, verify_embedding_dimension
from src.tools.reranker import DashScopeReranker
from src.tools.retrieval import HybridRetrievalService


@dataclass
class AppContainer:
    """运行期依赖集合。"""

    settings: Settings
    ready: bool
    readiness: ReadinessState
    run_store: RunStore
    event_store: EventStore
    run_manager: RunManager | None = None
    graph: Any | None = None
    knowledge_repository: PostgresKnowledgeRepository | None = None
    ingestion_manager: IngestionManager | None = None
    retrieval: HybridRetrievalService | None = None
    test_mode: bool = False
    close: Callable[[], Awaitable[None]] | None = None
    errors: list[str] = field(default_factory=list)


async def build_default_container(settings: Settings) -> AppContainer:
    """构建生产/真实运行容器。

    输入：配置。
    输出：`AppContainer`；失败时 ready=False 且保留明确 errors。
    作用：启动期验证 pgvector、embedding 维度和 checkpointer，不自动建库。
    实现：配置检查→连接池→schema→embedding ping→图→RunManager；退出栈统一清理。
    """
    config_errors = settings.config_errors(require_credentials=True)
    if config_errors:
        return _unready_container(settings, config_errors, "配置不完整")

    stack = AsyncExitStack()
    try:
        pool = await create_pool(settings)
        stack.push_async_callback(close_pool, pool)
        readiness = await check_database(pool)
        if not readiness.ready:
            await stack.aclose()
            return _unready_container(settings, readiness.errors, "数据库结构未就绪", readiness)

        embedder = DashScopeEmbedder(settings)
        try:
            await asyncio.wait_for(
                verify_embedding_dimension(embedder, settings.embedding_dim),
                timeout=settings.tool_timeout_seconds,
            )
        except Exception as exc:
            readiness.errors.append(f"embedding 探测失败：{type(exc).__name__}")
            readiness.ready = False
            await stack.aclose()
            return _unready_container(settings, readiness.errors, "embedding 不可用", readiness)

        checkpointer = await stack.enter_async_context(open_postgres_checkpointer(settings))
        repository = PostgresMaterialRepository(pool)
        reranker = DashScopeReranker(settings) if settings.rerank_enabled else None
        retrieval = HybridRetrievalService(repository, embedder, settings, reranker=reranker)
        services = GraphServices(
            settings=settings,
            decision_model=DeepSeekDecisionModel(settings),
            writer_model=DeepSeekWriterModel(settings),
            retrieval=retrieval,
            rule_provider=PostgresRuleProvider(pool),
        )
        graph = build_runtime_graph(services, checkpointer)
        run_store = PostgresRunStore(pool)
        event_store = PostgresEventStore(pool)
        manager = RunManager(settings, graph, run_store, event_store)
        await manager.recover_stale_runs()

        knowledge_repository = PostgresKnowledgeRepository(pool)
        ingestion_service = KnowledgeIngestionService(
            settings,
            knowledge_repository,
            embedder,
            on_published=retrieval.invalidate_index,
        )
        ingestion_manager = IngestionManager(ingestion_service)
        stack.push_async_callback(lambda: ingestion_manager.close())
        readiness.details["checkpointer"] = "postgres"
        readiness.details["rerank"] = settings.rerank_model if settings.rerank_enabled else "disabled"
        return AppContainer(
            settings=settings,
            ready=True,
            readiness=readiness,
            run_store=run_store,
            event_store=event_store,
            run_manager=manager,
            graph=graph,
            knowledge_repository=knowledge_repository,
            ingestion_manager=ingestion_manager,
            retrieval=retrieval,
            close=stack.aclose,
        )
    except Exception as exc:
        await stack.aclose()
        return _unready_container(
            settings,
            [f"运行依赖初始化失败：{type(exc).__name__}"],
            "启动失败",
        )


def _unready_container(
    settings: Settings,
    errors: list[str],
    reason: str,
    readiness: ReadinessState | None = None,
) -> AppContainer:
    """输入：错误列表；输出：不可运行容器。

    作用：让 `/healthz` `/readyz` 返回明确原因，同时避免使用内存存储伪装生产可用。
    实现：空内存存储 + run_manager=None；路由会返回 503。
    """
    from src.runtime.event_store import MemoryEventStore
    from src.runtime.run_store import MemoryRunStore

    state = readiness or ReadinessState()
    state.ready = False
    state.errors = list(errors)
    state.details.setdefault("reason", reason)
    return AppContainer(
        settings=settings,
        ready=False,
        readiness=state,
        run_store=MemoryRunStore(),
        event_store=MemoryEventStore(),
        run_manager=None,
        errors=list(errors),
    )
