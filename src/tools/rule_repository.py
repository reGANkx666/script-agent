"""品牌/平台规则数据访问。

输入：平台名。
输出：当前生效的品牌规则与平台规则。
作用：把规则来源与图节点解耦，测试可使用静态规则。
实现：Protocol + 静态实现；PostgreSQL 实现在 runtime 层注入连接池。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class RuleSnapshot:
    """一次规则快照。"""

    brand: list[dict[str, Any]] = field(default_factory=list)
    platform: list[dict[str, Any]] = field(default_factory=list)


class RuleProvider(Protocol):
    """规则提供器接口。"""

    async def get_rules(self, platform: str | None) -> RuleSnapshot:
        """输入：平台；输出：品牌与平台规则快照；作用：规则检查前加载。"""
        ...


class StaticRuleProvider:
    """内存规则提供器，测试和本地演练使用。"""

    def __init__(
        self,
        brand: list[dict[str, Any]] | None = None,
        platform: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        """输入：规则字典；输出：提供器；作用：固定可复现规则。"""
        self.brand = list(brand or [])
        self.platform = dict(platform or {})

    async def get_rules(self, platform: str | None) -> RuleSnapshot:
        """输入：平台名；输出：该平台的规则快照；作用：供 check_rules 节点调用。"""
        return RuleSnapshot(
            brand=list(self.brand),
            platform=list(self.platform.get(platform or "", [])),
        )


def default_rule_provider() -> StaticRuleProvider:
    """输出：P0 默认规则；作用：无数据库测试时的确定性规则集。"""
    return StaticRuleProvider(
        brand=[
            {"id": "BR01", "severity": "block", "rule": {"type": "fact_source_required"}},
            {
                "id": "BR02",
                "severity": "block",
                "rule": {"type": "forbid_effect_claim_without_data"},
            },
            {
                "id": "BR03",
                "severity": "block",
                "rule": {"type": "banned_terms", "terms": ["最", "第一", "绝对"]},
            },
            {"id": "BR04", "severity": "warn", "rule": {"type": "subtitle_clearance"}},
        ],
        platform={
            "抖音": [
                {
                    "code": "PLATFORM_DURATION",
                    "severity": "block",
                    "rule": {"max_sec": 180},
                }
            ]
        },
    )
