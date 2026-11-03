"""运行/事件存储的共享类型。

输入：API 请求、事件载荷。
输出：数据库与内存实现共用的 dataclass。
作用：避免 API、RunManager、存储层各自定义不同形状。
实现：dataclass + datetime；payload 使用普通 dict。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class RunRecord:
    """一次运行的持久化快照。"""

    run_id: str
    status: str
    request: dict[str, Any]
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    current_node: str | None = None
    model: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class EventRecord:
    """一条可回放事件。"""

    id: int
    run_id: str
    seq: int
    event_type: str
    node: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
