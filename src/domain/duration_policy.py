"""时长策略。

输入：脚本、目标时长、容差。
输出：每个分镜的语音/画面时长与总时长报告。
作用：提供可复算、无模型的 P0 时长判断。
实现：中文语音按每秒 4.5 个汉字估算，分镜取语音与画面停留较大值，再加转场。
"""

from __future__ import annotations

import math
import re
from typing import Any

from src.domain.models import DurationReport, Script

_CJK_RE = re.compile(r"[㐀-䶿一-鿿]")


def chinese_char_count(text: str) -> int:
    """输入：任意文本；输出：汉字数量；作用：作为语音时长分子；实现：正则统计 CJK 字符。"""
    return len(_CJK_RE.findall(text or ""))


def speech_seconds(text: str, chars_per_second: float = 4.5) -> int:
    """输入：文本与语速；输出：向上取整的语音秒数；作用：可解释的时长估算。"""
    count = chinese_char_count(text)
    if count == 0:
        return 0
    return math.ceil(count / chars_per_second)


def estimate_duration(
    script: Script,
    target_sec: int,
    tolerance_sec: int = 2,
    transition_sec: int = 0,
) -> DurationReport:
    """估算脚本时长。

    输入：脚本、目标秒数、容差、每个分镜的转场秒数。
    输出：`DurationReport`，包含逐分镜明细和计算依据。
    作用：供图节点和规则检查复用，不调用任何模型。
    实现：逐分镜计算 `max(语音秒数, 画面停留) + 转场`，求和后判断容差。
    """
    per_shot: list[dict[str, Any]] = []
    total = 0
    for shot in script.shots:
        speech = speech_seconds(shot.narration)
        estimated = max(speech, shot.visual_hold_sec) + transition_sec
        total += estimated
        per_shot.append(
            {
                "shot_id": shot.shot_id,
                "narration_chars": chinese_char_count(shot.narration),
                "speech_sec": speech,
                "visual_hold_sec": shot.visual_hold_sec,
                "transition_sec": transition_sec,
                "estimated_sec": estimated,
            }
        )
    return DurationReport(
        total_sec=total,
        target_sec=target_sec,
        tolerance_sec=tolerance_sec,
        passed=abs(total - target_sec) <= tolerance_sec,
        per_shot=per_shot,
        basis=(
            "speech_sec=ceil(chinese_chars/4.5); "
            "shot_sec=max(speech_sec,visual_hold_sec)+transition_sec; "
            "total_sec=sum(shot_sec)"
        ),
    )


def sync_shot_durations(script: Script, report: DurationReport) -> Script:
    """把时长报告回写到分镜。

    输入：脚本与时长报告。
    输出：新的脚本对象，`duration_sec` 与报告逐分镜估算一致。
    作用：保证最终结果满足 `total_sec == sum(shot.duration_sec)`。
    实现：按 shot_id 建映射；报告缺失时保留原值。
    """
    by_id = {item["shot_id"]: int(item["estimated_sec"]) for item in report.per_shot}
    shots = [
        shot.model_copy(update={"duration_sec": by_id.get(shot.shot_id, shot.duration_sec)})
        for shot in script.shots
    ]
    return script.model_copy(update={"shots": shots})


def report_with_total(script: Script, total_sec: int, target_sec: int, tolerance_sec: int = 2) -> DurationReport:
    """构造指定总时长的报告（仅测试替身使用）。

    输入：脚本、指定总秒数、目标与容差。
    输出：逐分镜合计等于 `total_sec` 的报告。
    作用：让验收场景可稳定复现 38/28/18 秒边界，而不依赖字符串长度。
    实现：把总时长按分镜数量均分，余数放到最后一个分镜。
    """
    count = len(script.shots)
    base, remainder = divmod(max(total_sec, 0), count)
    per_shot: list[dict[str, Any]] = []
    for index, shot in enumerate(script.shots):
        estimated = base + (remainder if index == count - 1 else 0)
        per_shot.append(
            {
                "shot_id": shot.shot_id,
                "narration_chars": chinese_char_count(shot.narration),
                "speech_sec": estimated,
                "visual_hold_sec": shot.visual_hold_sec,
                "transition_sec": 0,
                "estimated_sec": estimated,
            }
        )
    return DurationReport(
        total_sec=total_sec,
        target_sec=target_sec,
        tolerance_sec=tolerance_sec,
        passed=abs(total_sec - target_sec) <= tolerance_sec,
        per_shot=per_shot,
        basis="test override: total_sec distributes evenly across shots",
    )
