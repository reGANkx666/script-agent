"""P0 规则检查。

输入：脚本、Brief、素材、品牌规则、平台规则、时长报告和风险列表。
输出：`CheckReport`，block 违规会阻止 SUCCESS。
作用：把“无来源事实”“效果数据造假”“禁语”“平台时长”等约束做成确定性检查。
实现：规则按 code 独立检查，使用原文子串和正则，不引入 LLM 事实判断。
"""

from __future__ import annotations

import re
from typing import Any

from src.domain.fact_ledger import claim_map, material_ids
from src.domain.models import Brief, CheckReport, Claim, DurationReport, Material, Script, Violation

_EFFECT_RE = re.compile(
    r"(?:\d+(?:\.\d+)?\s*%|(?:提升|提高|增长|降低|减少|节省)\s*(?:\d+(?:\.\d+)?|百分之[零一二三四五六七八九十百]+))"
)
_NORMALIZE_RE = re.compile(r"[\s，。！？、,.!?；;：:\"'“”‘’()（）【】\[\]—-]")
_EFFECT_DATA_RE = re.compile(r"(?:\d+(?:\.\d+)?\s*%|提升|提高|增长|降低|减少)")


def _normalize(text: str) -> str:
    """输入：文本；输出：去空白和常见标点的紧凑文本；作用：降低子串误判。"""
    return _NORMALIZE_RE.sub("", text or "")


def _iter_rule_entries(rules: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """输入：规则行列表；输出：展开后的规则字典；作用：兼容 DB 行与纯规则对象。"""
    entries: list[dict[str, Any]] = []
    for raw in rules or []:
        rule = raw.get("rule", raw)
        if isinstance(rule, dict):
            entries.append({"id": raw.get("id") or raw.get("code"), "severity": raw.get("severity", "block"), **rule})
    return entries


def _material_has_effect_data(materials: list[Material]) -> bool:
    """输入：素材；输出：是否包含可引用的效果数据；作用：判断效果结论是否被允许。"""
    for material in materials:
        if "effect_data" in material.tags:
            return True
        if _EFFECT_DATA_RE.search(material.text):
            return True
    return False


def _script_texts(script: Script) -> list[tuple[str | None, str]]:
    """输入：脚本；输出：标题与各分镜可检查文本；作用：统一做禁语/效果扫描。"""
    texts: list[tuple[str | None, str]] = [(None, script.title)]
    for shot in script.shots:
        texts.append((shot.shot_id, f"{shot.visual}\n{shot.narration}\n{shot.subtitle}"))
    return texts


def check_rules(
    script: Script,
    brief: Brief,
    materials: list[Material],
    brand_rules: list[dict[str, Any]] | None = None,
    platform_rules: list[dict[str, Any]] | None = None,
    *,
    duration: DurationReport | None = None,
    claims: list[Claim] | None = None,
    risks: list[dict[str, Any]] | None = None,
) -> CheckReport:
    """执行 P0 规则检查。

    输入：脚本、需求、素材、品牌/平台规则、可选时长、声明和风险。
    输出：规则报告；block 违规会阻止最终 SUCCESS。
    作用：集中所有确定性内容约束，供图节点和最终校验调用。
    实现：逐规则生成 `Violation`，最后按 severity 统计 passed。
    """
    violations: list[Violation] = []
    claims = claims or []
    risks = risks or []
    claims_by_id = claim_map(claims)
    allowed_sources = material_ids(materials)
    has_effect_data = _material_has_effect_data(materials)
    entries = _iter_rule_entries(brand_rules)

    # 1) 时长存在性与目标/平台上限。
    if duration is None:
        violations.append(
            Violation(code="DURATION_MISSING", severity="block", message="没有时长报告")
        )
    else:
        if not duration.passed:
            violations.append(
                Violation(
                    code="DURATION_OVER",
                    severity="block",
                    message="总时长超出目标容差",
                    evidence={
                        "total_sec": duration.total_sec,
                        "target_sec": duration.target_sec,
                        "tolerance_sec": duration.tolerance_sec,
                    },
                    suggested_fix="压缩口播或减少分镜",
                )
            )
        platform_cap = _platform_duration_cap(platform_rules or [])
        if platform_cap is not None and duration.total_sec > platform_cap:
            violations.append(
                Violation(
                    code="PLATFORM_LIMIT",
                    severity="block",
                    message=f"平台时长限制为 {platform_cap} 秒",
                    evidence={"total_sec": duration.total_sec, "max_sec": platform_cap},
                )
            )

    # 2) 禁语规则。
    banned_terms: list[str] = []
    for entry in entries:
        if entry.get("type") == "banned_terms":
            banned_terms.extend(str(term) for term in entry.get("terms", []) if str(term))
    for shot_id, text in _script_texts(script):
        hit_terms = [term for term in banned_terms if term in text]
        if hit_terms:
            violations.append(
                Violation(
                    code="BANNED_TERM",
                    severity="block",
                    shot_id=shot_id,
                    message=f"命中品牌禁语：{', '.join(hit_terms)}",
                    evidence={"terms": hit_terms},
                    suggested_fix="改写为客观描述",
                )
            )

    # 3) 声明来源与原文一致性。
    for shot in script.shots:
        for claim_id in shot.claim_ids:
            claim = claims_by_id.get(claim_id)
            if claim is None:
                violations.append(
                    Violation(
                        code="UNSUPPORTED_CLAIM",
                        severity="block",
                        shot_id=shot.shot_id,
                        message=f"claim {claim_id} 不存在",
                        evidence={"claim_id": claim_id},
                    )
                )
                continue
            if not claim.approved or claim.source_id not in allowed_sources:
                violations.append(
                    Violation(
                        code="UNSUPPORTED_CLAIM",
                        severity="block",
                        shot_id=shot.shot_id,
                        message="claim 未批准或来源不在素材白名单",
                        evidence={"claim_id": claim_id, "source_id": claim.source_id},
                    )
                )
            if _normalize(claim.text) not in _normalize(shot.narration):
                violations.append(
                    Violation(
                        code="UNSUPPORTED_CLAIM",
                        severity="block",
                        shot_id=shot.shot_id,
                        message="口播未包含 claim 原文，无法证明事实来源",
                        evidence={"claim_id": claim_id, "claim_text": claim.text},
                        suggested_fix="在口播中保留 claim 原文，或删除该 claim 引用",
                    )
                )

    # 4) 无数据的效果结论。
    if not has_effect_data:
        for shot_id, text in _script_texts(script):
            match = _EFFECT_RE.search(text)
            if match:
                violations.append(
                    Violation(
                        code="EFFECT_WITHOUT_DATA",
                        severity="block",
                        shot_id=shot_id,
                        message="出现效果数据结论，但素材没有效果数据",
                        evidence={"matched": match.group(0)},
                        suggested_fix="删除效果百分比或补充可引用数据",
                    )
                )

    # 5) 字幕密度与默认字段提示。
    for shot in script.shots:
        seconds = max(shot.duration_sec, 1)
        chars = len(re.sub(r"\s", "", shot.subtitle))
        if chars / seconds > 8:
            violations.append(
                Violation(
                    code="SUBTITLE_TOO_LONG",
                    severity="warn",
                    shot_id=shot.shot_id,
                    message="字幕每秒字符数过高",
                    evidence={"chars": chars, "duration_sec": seconds},
                )
            )
    for risk in risks:
        if risk.get("code") == "DEFAULTED_FIELD":
            violations.append(
                Violation(
                    code="DEFAULTED_FIELD",
                    severity="info",
                    message=f"{risk.get('field')} 使用了默认值 {risk.get('value')}",
                    evidence=risk,
                )
            )

    return CheckReport(
        passed=not any(item.severity == "block" for item in violations),
        violations=violations,
    )


def _platform_duration_cap(platform_rules: list[dict[str, Any]]) -> int | None:
    """输入：某平台规则行；输出：最大时长或 None；作用：避免不同规则表示方式泄漏到主流程。"""
    cap: int | None = None
    for raw in platform_rules:
        rule = raw.get("rule", raw)
        if not isinstance(rule, dict):
            continue
        value = rule.get("max_sec", raw.get("max_sec"))
        if isinstance(value, (int, float)):
            cap = int(value) if cap is None else min(cap, int(value))
    return cap
