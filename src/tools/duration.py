"""时长工具入口。

输入：脚本、目标秒数、容差。
输出：`DurationReport`。
作用：给图节点和外部调用者提供稳定工具名；实现完全委托纯函数策略。
"""

from __future__ import annotations

from src.domain.duration_policy import estimate_duration
from src.domain.models import DurationReport, Script


def estimate_duration_tool(script: Script, target_sec: int, tolerance_sec: int = 2) -> DurationReport:
    """输入：脚本/目标/容差；输出：时长报告；作用：无模型、可复算；实现：调用领域纯函数。"""
    return estimate_duration(script, target_sec=target_sec, tolerance_sec=tolerance_sec)
