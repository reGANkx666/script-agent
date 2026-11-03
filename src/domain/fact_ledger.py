"""事实账本。

输入：检索得到的素材和已有声明。
输出：去重后的 `Claim` 列表及素材 ID 白名单。
作用：保证脚本只能引用真实来源，禁止模型自由生成 source_id。
实现：以素材 ID 为键生成稳定 claim_id，不调用模型。
"""

from __future__ import annotations

from src.domain.models import Claim, Material


def claims_from_materials(materials: list[Material], existing: list[Claim] | None = None) -> list[Claim]:
    """从素材构建事实声明。

    输入：素材列表、已有声明（保留已有 claim_id）。
    输出：按 source_id 去重且顺序稳定的声明列表。
    作用：把检索结果转成 Writer 可引用的白名单。
    实现：同一素材只生成一条 claim；文本直接使用素材原文，避免二次改写造成漂移。
    """
    existing = existing or []
    by_source = {claim.source_id: claim for claim in existing}
    claims: list[Claim] = []
    seen: set[str] = set()
    for material in materials:
        if material.id in seen:
            continue
        seen.add(material.id)
        claim = by_source.get(material.id)
        if claim is None:
            claim = Claim(
                claim_id=f"C-{material.id}",
                text=material.text,
                source_id=material.id,
                approved=True,
            )
        claims.append(claim)
    return claims


def material_ids(materials: list[Material]) -> set[str]:
    """输入：素材列表；输出：合法 source_id 集合；作用：校验模型输出。"""
    return {material.id for material in materials}


def claim_map(claims: list[Claim]) -> dict[str, Claim]:
    """输入：声明列表；输出：claim_id 到声明的映射；作用：快速校验分镜引用。"""
    return {claim.claim_id: claim for claim in claims}
