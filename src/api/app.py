"""FastAPI 应用入口。

输入：配置、可选注入容器。
输出：ASGI app。
作用：组装路由、lifespan、健康/就绪检查。
实现：启动期构建容器；测试可直接注入 AppContainer。
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Awaitable, Callable

# psycopg async 不支持 Windows 默认的 ProactorEventLoop；必须在创建事件循环前切换。
if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from src.api.routes_knowledge import router as knowledge_router
from src.api.routes_runs import router as runs_router
from src.api.routes_test import router as test_router
from src.api.schemas import HealthResponse, ReadinessResponse
from src.config import Settings, get_settings
from src.runtime.container import AppContainer, build_default_container

ContainerFactory = Callable[[Settings], Awaitable[AppContainer]]


def create_app(
    settings: Settings | None = None,
    *,
    container: AppContainer | None = None,
    container_factory: ContainerFactory | None = None,
) -> FastAPI:
    """创建 FastAPI 应用。

    输入：配置、可选现成容器或异步容器工厂。
    输出：ASGI app。
    作用：生产使用默认构建；测试注入 Memory 容器。
    实现：lifespan 负责构建/关闭；路由从 app.state.container 读取依赖。
    """
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """管理应用资源生命周期。

        输入：app。
        输出：无。
        作用：启动构建容器、结束关闭连接池/checkpointer。
        实现：已有容器直接使用；否则调用工厂或默认构建器。
        """
        current = getattr(app.state, "container", None)
        if current is None:
            if container_factory is not None:
                current = await container_factory(resolved_settings)
            else:
                current = await build_default_container(resolved_settings)
            app.state.container = current
        yield
        if current is not None and current.close is not None:
            await current.close()

    app = FastAPI(
        title="短视频脚本 Agent",
        version="0.1.0",
        description="确定性 LangGraph 运行时 + DeepSeek 动作/写作模型",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.container = container
    app.include_router(runs_router)
    app.include_router(knowledge_router)
    app.include_router(test_router)

    web_dir = Path(__file__).resolve().parents[2] / "web"
    if web_dir.is_dir():
        app.mount("/app", StaticFiles(directory=str(web_dir), html=True), name="control-room")

        @app.get("/", include_in_schema=False)
        async def root() -> RedirectResponse:
            """把根路径导航到可交互控制台。"""
            return RedirectResponse(url="/app/")

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz(request: Request):
        """进程/配置健康检查。

        输入：无。
        输出：200 或 503。
        作用：配置缺 Key 时明确报错，而不是假装健康。
        实现：检查配置；Mock 测试容器可跳过真实凭据。
        """
        current: AppContainer | None = getattr(request.app.state, "container", None)
        test_mode = bool(current and current.test_mode)
        errors: list[str] = []
        if not test_mode:
            errors = resolved_settings.config_errors(require_credentials=True)
        body = HealthResponse(
            status="ok" if not errors else "error",
            app_env=resolved_settings.app_env,
            errors=errors,
        )
        return JSONResponse(status_code=200 if not errors else 503, content=body.model_dump())

    @app.get("/readyz", response_model=ReadinessResponse)
    async def readyz(request: Request):
        """依赖就绪检查。

        输入：无。
        输出：200 或 503。
        作用：DB/pgvector/embedding/checkpointer 未就绪时阻止接流量。
        实现：读取启动期 ReadinessState。
        """
        current: AppContainer | None = getattr(request.app.state, "container", None)
        if current is None:
            body = ReadinessResponse(status="starting", ready=False, errors=["应用尚未初始化"])
            return JSONResponse(status_code=503, content=body.model_dump())
        body = ReadinessResponse(
            status="ready" if current.ready else "not_ready",
            ready=current.ready,
            errors=current.readiness.errors,
            details=current.readiness.details,
        )
        return JSONResponse(status_code=200 if current.ready else 503, content=body.model_dump())

    return app


app = create_app()
