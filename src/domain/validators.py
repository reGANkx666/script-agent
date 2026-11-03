"""输出不变量校验。

输入：脚本、声明、素材、时长报告和规则检查结果。
输出：独立的校验报告。
作用：作为 finalize 前的最后防线，确保无来源事实、假素材 ID 或时长矛盾不会以 SUCCESS 返回。
实现：全部使用确定性字符串/数值检查，不调用 LLM。
"""

from __future__ import annotations

from src.domain.fact_ledger import claim_map, material_ids
from src.domain.models import CheckReport, Claim, DurationReport, Material, Script, Violation


def validate_output(
    script: Script,
    claims: list[Claim],
    materials: list[Material],
    duration: DurationReport | None,
    check: CheckReport | None,
    target_sec: int | None = None,
    tolerance_sec: int = 2,
) -> CheckReport:
    """校验最终输出不变量。

    输入：最终脚本、声明白名单、素材、时长/规则报告、目标时长与容差。
    输出：汇总的 `CheckReport`，任一 block 违规都禁止 SUCCESS。
    作用：防止模型编造来源或输出自相矛盾的结果。
    实现：逐项检查 claim 存在性、素材来源、时长求和、目标容差和已有 block 违规。
    """
    violations: list[Violation] = []
    claims_by_id = claim_map(claims)
    allowed_sources = material_ids(materials)

    # 所有 claim 都必须有合法来源，即使它没有被最终分镜引用。
    for claim in claims:
        if claim.source_id not in allowed_sources:
            violations.append(
                Violation(
                    code="SOURCE_NOT_FOUND",
                    severity="block",
                    message=f"claim {claim.claim_id} 的来源不在素材白名单",
                    evidence={"claim_id": claim.claim_id, "source_id": claim.source_id},
                )
            )

    shot_ids = set()
    for shot in script.shots:
        if shot.shot_id in shot_ids:
            violations.append(
                Violation(
                    code="DUPLICATE_SHOT_ID",
                    severity="block",
                    shot_id=shot.shot_id,
                    message="分镜 ID 重复",
                )
            )
        shot_ids.add(shot.shot_id)
        for claim_id in shot.claim_ids:
            claim = claims_by_id.get(claim_id)
            if claim is None:
                violations.append(
                    Violation(
                        code="CLAIM_NOT_FOUND",
                        severity="block",
                        shot_id=shot.shot_id,
                        message=f"claim {claim_id} 不存在",
                        evidence={"claim_id": claim_id},
                    )
                )
                continue
            if not claim.approved:
                violations.append(
                    Violation(
                        code="CLAIM_NOT_APPROVED",
                        severity="block",
                        shot_id=shot.shot_id,
                        message=f"claim {claim_id} 未批准",
                        evidence={"claim_id": claim_id},
                    )
                )
            if claim.source_id not in allowed_sources:
                violations.append(
                    Violation(
                        code="SOURCE_NOT_FOUND",
                        severity="block",
                        shot_id=shot.shot_id,
                        message=f"来源 {claim.source_id} 不在素材白名单",
                        evidence={"claim_id": claim_id, "source_id": claim.source_id},
                    )
                )

    if duration is None:
        violations.append(
            Violation(code="DURATION_MISSING", severity="block", message="缺少时长报告")
        )
    else:
        total_shots = sum(shot.duration_sec for shot in script.shots)
        if duration.total_sec != total_shots:
            violations.append(
                Violation(
                    code="DURATION_INCONSISTENT",
                    severity="block",
                    message="时长报告总和与分镜时长不一致",
                    evidence={"report_total": duration.total_sec, "shot_total": total_shots},
                )
            )
        tolerance = tolerance_sec if target_sec is None else tolerance_sec
        expected_target = duration.target_sec if target_sec is None else target_sec
        if abs(duration.total_sec - expected_target) > tolerance:
            violations.append(
                Violation(
                    code="DURATION_TARGET_MISMATCH",
                    severity="block",
                    message="总时长超出目标容差",
                    evidence={
                        "total_sec": duration.total_sec,
                        "target_sec": expected_target,
                        "tolerance_sec": tolerance,
                    },
                )
            )
        if duration.passed != (abs(duration.total_sec - expected_target) <= tolerance):
            violations.append(
                Violation(
                    code="DURATION_FLAG_INCONSISTENT",
                    severity="block",
                    message="时长 passed 标记与数值判断不一致",
                )
            )

    if check is not None:
        block_items = [item for item in check.violations if item.severity == "block"]
        if block_items:
            violations.append(
                Violation(
                    code="BLOCK_VIOLATION",
                    severity="block",
                    message=f"存在 {len(block_items)} 条 block 级规则违规",
                    evidence={"codes": [item.code for item in block_items]},
                )
            )

    severity_rank = {"info": 0, "warn": 1, "block": 2}
    deduped: list[Violation] = []
    seen: set[tuple[str, str | None]] = set()
    for violation in violations:
        key = (violation.code, violation.shot_id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(violation)
    deduped.sort(key=lambda item: severity_rank[item.severity], reverse=True)
    return CheckReport(passed=not any(item.severity == "block" for item in deduped), violations=deduped)
