"""ASGI 入口别名，便于 `uvicorn src.main:app` 启动。"""

from src.api.app import app

__all__ = ["app"]
