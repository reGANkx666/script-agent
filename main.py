"""项目启动入口。

输入：环境变量 / `.env`。
输出：Uvicorn 托管的 FastAPI 应用。
作用：兼容 PyCharm 直接运行与 `python main.py`。
实现：委托 `src.api.app:app`。
"""

from __future__ import annotations

import uvicorn

from src.api.app import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
