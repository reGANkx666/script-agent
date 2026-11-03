"""FastAPI 依赖。

输入：请求。
输出：应用容器 / RunManager。
作用：路由不从全局变量取状态，便于测试注入。
实现：从 `app.state.container` 读取；不可用时抛 503。
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from src.runtime.container import AppContainer
from src.runtime.run_manager import RunManager


def get_container(request: Request) -> AppContainer:
    """输入：请求；输出：容器；作用：路由依赖；实现：检查 app.state。"""
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(status_code=503, detail="应用尚未初始化")
    return container


def require_manager(container: AppContainer) -> RunManager:
    """输入：容器；输出：RunManager；作用：阻止未就绪实例接收运行；实现：显式 503。"""
    if not container.ready or container.run_manager is None:
        raise HTTPException(
            status_code=503,
            detail={"message": "服务未就绪", "errors": container.readiness.errors},
        )
    return container.run_manager
