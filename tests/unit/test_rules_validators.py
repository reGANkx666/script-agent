"""规则与输出不变量测试。"""

from __future__ import annotations

from src.domain.models import Claim, Material
from src.domain.duration_policy import report_with_total
from src.domain.validators import validate_output
from src.tools.duration import estimate_duration_tool
from src.tools.rules import check_rules
from tests.helpers import make_script


def _material() -> Material:
    """输出：P01 素材。"""
    return Material(id="P01", text="产品支持字幕功能", source="产品说明 v1.2")


def _claim() -> Claim:
    """输出：P01 声明。"""
    return Claim(claim_id="C-P01", text="产品支持字幕功能", source_id="P01")


def _brief():
    """输出：完整 Brief。"""
    from src.domain.models import Brief

    return Brief(topic="字幕", platform="抖音", target_duration_sec=30)


def test_rules_pass_with_supported_claim() -> None:
    """输入：有来源、时长通过；输出：无 block。"""
    script = make_script(narration="产品支持字幕功能")
    duration = report_with_total(script, 30, 30, 2)
    report = check_rules(
        script,
        _brief(),
        [_material()],
        brand_rules=[{"id": "BR03", "rule": {"type": "banned_terms", "terms": ["最"]}}],
        platform_rules=[{"code": "D", "rule": {"max_sec": 180}}],
        duration=duration,
        claims=[_claim()],
    )
    assert report.passed is True


def test_banned_term_and_effect_without_data() -> None:
    """输入：禁语和效果百分比；输出：两个 block 违规。"""
    script = make_script(narration="产品支持字幕功能，绝对能提升效率 80%")
    duration = estimate_duration_tool(script, 30, 10)
    rules = [
        {"id": "BR03", "rule": {"type": "banned_terms", "terms": ["最", "绝对"]}},
    ]
    report = check_rules(
        script,
        _brief(),
        [_material()],
        brand_rules=rules,
        platform_rules=[],
        duration=duration,
        claims=[_claim()],
    )
    codes = {item.code for item in report.violations}
    assert "BANNED_TERM" in codes
    assert "EFFECT_WITHOUT_DATA" in codes
    assert report.passed is False


def test_unsupported_claim_paraphrase_is_blocked() -> None:
    """输入：口播改写 claim 原文；输出：UNSUPPORTED_CLAIM。"""
    script = make_script(narration="这个产品可以加字幕")
    duration = estimate_duration_tool(script, 30, 15)
    report = check_rules(
        script,
        _brief(),
        [_material()],
        brand_rules=[],
        platform_rules=[],
        duration=duration,
        claims=[_claim()],
    )
    assert "UNSUPPORTED_CLAIM" in {item.code for item in report.violations}


def test_output_validator_rejects_fake_source() -> None:
    """输入：引用不存在的来源；输出：SOURCE_NOT_FOUND + block。"""
    script = make_script(narration="产品支持字幕功能")
    duration = estimate_duration_tool(script, 30, 15)
    fake_claim = Claim(claim_id="C-FAKE", text="产品支持字幕功能", source_id="P99")
    script.shots[0].claim_ids = ["C-FAKE"]
    report = validate_output(
        script,
        [fake_claim],
        [_material()],
        duration,
        None,
        target_sec=30,
        tolerance_sec=15,
    )
    assert report.passed is False
    assert "SOURCE_NOT_FOUND" in {item.code for item in report.violations}


def test_defaulted_field_is_info_only() -> None:
    """输入：默认受众风险；输出：info 且不阻断。"""
    script = make_script(narration="产品支持字幕功能")
    duration = report_with_total(script, 30, 30, 2)
    report = check_rules(
        script,
        _brief(),
        [_material()],
        brand_rules=[],
        platform_rules=[],
        duration=duration,
        claims=[_claim()],
        risks=[{"code": "DEFAULTED_FIELD", "field": "audience", "value": "入门用户"}],
    )
    item = next(v for v in report.violations if v.code == "DEFAULTED_FIELD")
    assert item.severity == "info"
    assert report.passed is True
