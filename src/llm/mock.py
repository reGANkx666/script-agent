"""测试用 Mock 模型。

输入：预置动作队列或脚本队列。
输出：确定性动作/脚本。
作用：让 CI 与验收场景不依赖真实模型，也不让真实模型决定断言结果。
实现：顺序出队；耗尽时抛出 `ModelError`，明确暴露场景编排错误。
"""

from __future__ import annotations

from src.domain.models import ActionDecision, Brief, Claim, Script
from src.llm.base import DecisionContext, DecisionModel, ModelError, WriterContext, WriterModel


class MockDecisionModel:
    """队列式决策模型。"""

    def __init__(self, decisions: list[ActionDecision]) -> None:
        """输入：动作队列；输出：Mock；作用：固定图路由；实现：复制列表避免外部修改。"""
        self._queue = list(decisions)
        self.calls: list[DecisionContext] = []

    async def decide(self, context: DecisionContext) -> ActionDecision:
        """输入：决策上下文；输出：下一条动作；作用：可复现测试；实现：FIFO 出队。"""
        self.calls.append(context)
        if not self._queue:
            raise ModelError("mock action queue exhausted")
        return self._queue.pop(0)


class ScriptedWriterModel:
    """队列式 Writer。"""

    def __init__(self, scripts: list[Script]) -> None:
        """输入：脚本队列；输出：Mock Writer；作用：固定首稿/修订稿。"""
        self._queue = list(scripts)
        self.calls: list[WriterContext] = []

    async def write(self, context: WriterContext) -> Script:
        """输入：Writer 上下文；输出：下一条脚本；作用：可复现测试；实现：FIFO 出队。"""
        self.calls.append(context)
        if not self._queue:
            raise ModelError("mock writer queue exhausted")
        return self._queue.pop(0)


class TemplateWriterModel:
    """无外部模型的最小 Writer。

    输入：Writer 上下文；输出：只引用 approved claims 的模板脚本。
    作用：测试 API 未提供脚本队列时的确定性兜底。
    实现：每个 claim 一个分镜，口播原样包含 claim 文本，避免事实改写。
    """

    def __init__(self, target_sec: int = 30) -> None:
        """输入：目标时长；输出：Writer；作用：决定分镜数量；实现：按 claim 数拆分。"""
        self.target_sec = target_sec

    async def write(self, context: WriterContext) -> Script:
        """输入：Writer 上下文；输出：合法模板脚本；作用：跑通无 Mock Writer 的场景。

        实现：取 approved claims；无 claim 时仍生成不含事实的总览镜头，
        但最终规则/输出校验会显式失败，而不是伪造素材来源。
        """
        claims = [claim for claim in context.claims if claim.approved]
        if not claims:
            claims = [Claim(claim_id="C-EMPTY", text="", source_id="", approved=False)]
        shots = []
        for index, claim in enumerate(claims, start=1):
            narration = claim.text or f"围绕{context.brief.topic}展开说明。"
            shots.append(
                {
                    "shot_id": f"S{index:02d}",
                    "visual": f"真实产品使用画面 {index}",
                    "narration": narration,
                    "subtitle": narration[:12],
                    "duration_sec": max(2, self.target_sec // max(len(claims), 1)),
                    "visual_hold_sec": 0,
                    "claim_ids": [claim.claim_id] if claim.claim_id != "C-EMPTY" else [],
                }
            )
        return Script(
            script_id=f"draft_{context.revision + 1:02d}",
            title=f"{context.brief.topic}：实用入门",
            shots=shots,
        )


# 显式导出 Protocol 名称，便于类型检查工具识别本模块的职责。
DecisionModelProtocol = DecisionModel
WriterModelProtocol = WriterModel


def brief_with_defaults(brief: Brief) -> Brief:
    """输入：Brief；输出：不变副本；作用：测试辅助；实现：保留用户显式值。"""
    return brief.model_copy(deep=True)
