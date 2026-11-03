"""首稿生成节点。"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from src.graph.nodes.common import approved_claims, emit
from src.graph.services import GraphServices
from src.graph.state import AgentState
from src.llm.base import WriterContext


def make_draft_node(services: GraphServices):
    """输入：依赖容器；输出：draft_script 节点。"""

    async def draft_script(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        """调用 Writer 生成首稿。

        输入：Brief、approved claims。
        输出：draft、tool_calls 与观察。
        作用：把事实白名单投影给模型并阻止引用未知 claim。
        实现：Writer 调用→Pydantic 校验→claim/source 白名单检查；失败只记录显式错误。
        """
        brief = state["brief"]
        claims = approved_claims(state)
        materials = list(state.get("materials", []))
        tool_calls = int(state.get("tool_calls", 0)) + 1
        await emit(config, "tool.called", "draft_script", {"claim_ids": [c.claim_id for c in claims]})
        try:
            script = await services.writer_model.write(
                WriterContext(
                    brief=brief,
                    claims=claims,
                    materials=materials,
                    contexts=list(state.get("contexts", [])),
                    constraints=services.make_writer_constraints(brief),
                )
            )
            _assert_claim_whitelist(script, claims)
        except Exception as exc:
            observation = {
                "action": "draft_script",
                "status": "ERROR",
                "error": f"Writer 输出不可用：{type(exc).__name__}",
                "error_status": "MODEL_ERROR",
            }
            await emit(
                config,
                "tool.result",
                "draft_script",
                {"status": "ERROR", "error_type": type(exc).__name__},
            )
            return {
                "tool_calls": tool_calls,
                "pending_action": None,
                "last_observation": observation,
            }

        observation = {
            "action": "draft_script",
            "status": "OK",
            "script_id": script.script_id,
            "shot_count": len(script.shots),
            "claim_ids": sorted({claim_id for shot in script.shots for claim_id in shot.claim_ids}),
        }
        await emit(config, "tool.result", "draft_script", observation)
        return {
            "draft": script,
            "duration": None,
            "check": None,
            "tool_calls": tool_calls,
            "pending_action": None,
            "last_observation": observation,
        }

    return draft_script


def _assert_claim_whitelist(script, claims) -> None:
    """输入：脚本与 approved claims；输出：无；作用：拒绝不存在的 claim；实现：集合包含检查。"""
    allowed = {claim.claim_id for claim in claims}
    used = {claim_id for shot in script.shots for claim_id in shot.claim_ids}
    unknown = used - allowed
    if unknown:
        raise ValueError("script references unknown claims")
