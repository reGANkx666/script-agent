"""Windows-safe FastAPI launcher.

psycopg async does not support the default Windows ProactorEventLoop. Python
3.12+ lets us pass a loop factory to asyncio.run, so the Uvicorn server is
started explicitly on a SelectorEventLoop instead of relying on the deprecated
global policy.
"""

from __future__ import annotations

import asyncio
import os
import selectors

import uvicorn


async def _serve() -> None:
    """启动 Uvicorn server，不自己创建事件循环。"""
    config = uvicorn.Config(
        "src.api.app:app",
        host="127.0.0.1",
        port=int(os.getenv("PORT", "8000")),
        loop="asyncio",
        reload=False,
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(
        _serve(),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )
