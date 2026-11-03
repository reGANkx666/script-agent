"""时长公式单元测试。"""

from __future__ import annotations

from src.domain.duration_policy import (
    chinese_char_count,
    estimate_duration,
    report_with_total,
    speech_seconds,
    sync_shot_durations,
)
from tests.helpers import make_script


def test_boundary_38_28_18() -> None:
    """输入：171/126/81 个汉字；输出：38/28/18 秒；作用：固定 M2 边界。"""
    for chars, expected in [(171, 38), (126, 28), (81, 18)]:
        script = make_script(narration="字" * chars)
        report = estimate_duration(script, target_sec=expected, tolerance_sec=0)
        assert chinese_char_count(script.shots[0].narration) == chars
        assert speech_seconds(script.shots[0].narration) == expected
        assert report.total_sec == expected


def test_tolerance_and_visual_hold() -> None:
    """输入：28 秒脚本，目标 30；输出：容差内通过，画面停留可抬升时长。"""
    report = estimate_duration(make_script(narration="字" * 126), target_sec=30, tolerance_sec=2)
    assert report.passed is True
    held = estimate_duration(
        make_script(narration="短", visual_hold_sec=10), target_sec=10, tolerance_sec=0
    )
    assert held.total_sec == 10


def test_sync_shot_durations_and_override() -> None:
    """输入：时长覆盖；输出：事件报告与分镜 duration 一致。"""
    script = make_script(narration="短句")
    report = report_with_total(script, 38, 30, 2)
    synced = sync_shot_durations(script, report)
    assert sum(shot.duration_sec for shot in synced.shots) == 38
    assert report.passed is False


def test_punctuation_not_counted() -> None:
    """输入：标点文本；输出：标点不计入语音字数。"""
    assert chinese_char_count("你好，世界！123") == 4
