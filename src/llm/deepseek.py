"""DeepSeek 模型适配。

输入：投影后的决策/写作上下文。
输出：校验过的 `ActionDecision` 或 `Script`。
作用：通过 OpenAI 兼容协议接入 `deepseek-chat`，把模型能力限制在动作选择和写作。
实现：LangChain `ChatOpenAI` + `bind_tools`；模型输出必须恰好包含一个合法工具调用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from src.config import Settings
from src.domain.models import ActionDecision, Script
from src.domain.status import MODEL_SELECTABLE_FAILURES, ActionName
from src.llm.base import (
    DecisionContext,
    ModelError,
    WriterContext,
    decision_context_payload,
    writer_context_payload,
)

_PROMPT_DIR = Path(__file__).parent / "prompts"

ACTION_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_materials",
            "description": "检索可引用素材",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
                    "reason": {"type": "string"},
                },
                "required": ["query", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_script",
            "description": "基于已有 claims 生成首稿",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "estimate_duration",
            "description": "估算当前脚本时长",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_rules",
            "description": "执行品牌和平台规则检查",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "revise_script",
            "description": "按 violations 修订脚本",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "向用户询问缺失信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "missing_fields": {"type": "array", "items": {"type": "string"}},
                    "question": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["missing_fields", "question", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalize",
            "description": "输出最终脚本",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fail",
            "description": "以显式原因终止",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": sorted(str(item) for item in MODEL_SELECTABLE_FAILURES),
                    },
                    "message": {"type": "string"},
                    "suggestions": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["status", "message", "reason"],
            },
        },
    },
]

_SUBMIT_SCRIPT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_script",
        "description": "提交完整脚本",
        "parameters": Script.model_json_schema(),
    },
}


def _read_prompt(name: str) -> str:
    """输入：Prompt 文件名；输出：文本；作用：集中加载；实现：读取同目录 md。"""
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def _make_model(settings: Settings) -> ChatOpenAI:
    """输入：配置；输出：ChatOpenAI；作用：统一 DeepSeek 连接参数；实现：OpenAI 兼容配置。"""
    if not settings.deepseek_api_key:
        raise ModelError("DEEPSEEK_API_KEY 未配置")
    return ChatOpenAI(
        model=settings.deepseek_model,
        base_url=settings.deepseek_base_url,
        api_key=settings.deepseek_api_key,
        temperature=0.2,
        timeout=60,
        max_retries=1,
    )


def _parse_decision(tool_call: dict[str, Any]) -> ActionDecision:
    """输入：单个工具调用；输出：动作决策；作用：隔离模型协议差异。

    实现：校验动作名和参数，把 `reason` 提升为 `basis`；失败抛 `ModelError`。
    """
    name = tool_call.get("name")
    allowed = {str(action) for action in ActionName}
    if name not in allowed:
        raise ModelError(f"模型返回未知动作：{name}")
    arguments = tool_call.get("args") or {}
    if not isinstance(arguments, dict):
        raise ModelError("工具参数必须是对象")
    arguments = dict(arguments)
    reason = str(arguments.pop("reason", "") or "")
    action = ActionName(name)
    if action is ActionName.SEARCH_MATERIALS:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ModelError("search_materials 缺少 query")
        arguments["query"] = query
        arguments["top_k"] = max(1, min(int(arguments.get("top_k", 5)), 20))
    if action is ActionName.ASK_USER:
        missing = arguments.get("missing_fields")
        if not isinstance(missing, list) or not missing:
            raise ModelError("ask_user 缺少 missing_fields")
        if not str(arguments.get("question", "")).strip():
            raise ModelError("ask_user 缺少 question")
    if action is ActionName.FAIL:
        status = str(arguments.get("status", ""))
        if status not in {str(item) for item in MODEL_SELECTABLE_FAILURES}:
            raise ModelError(f"fail 状态不合法：{status}")
        if not str(arguments.get("message", "")).strip():
            raise ModelError("fail 缺少 message")
    return ActionDecision(action=action, args=arguments, basis=reason)


class DeepSeekDecisionModel:
    """DeepSeek 动作选择适配器。"""

    def __init__(self, settings: Settings) -> None:
        """输入：配置；输出：适配器；作用：按 `DECISION_MODE` 选择 tools/json 模式。"""
        self.settings = settings
        self._model = _make_model(settings)
        self._tools_model = self._model.bind_tools(ACTION_TOOLS, tool_choice="auto")
        self._json_model = self._model.with_structured_output(ActionDecision)

    async def decide(self, context: DecisionContext) -> ActionDecision:
        """选择动作。

        输入：决策上下文。
        输出：合法 `ActionDecision`。
        作用：图节点唯一的模型决策入口。
        实现：tools 模式要求恰好一个 tool_call；非法输出重试一次后转 `MODEL_ERROR`。
        """
        payload = decision_context_payload(context)
        prompt = _read_prompt("decide.md").replace(
            "{context}", json.dumps(payload, ensure_ascii=False)[:12000]
        )
        last_error: Exception | None = None
        for _ in range(2):
            try:
                if self.settings.decision_mode == "json":
                    result = await self._json_model.ainvoke(
                        [SystemMessage(content=prompt), HumanMessage(content="请输出一个动作。")]
                    )
                    if isinstance(result, ActionDecision):
                        decision = result
                    else:
                        decision = ActionDecision.model_validate(result)
                    name = str(decision.action)
                    return _parse_decision(
                        {"name": name, "args": {**decision.args, "reason": decision.basis}}
                    )
                response = await self._tools_model.ainvoke(
                    [SystemMessage(content=prompt), HumanMessage(content="请选择下一步动作。")]
                )
                calls = list(getattr(response, "tool_calls", []) or [])
                if len(calls) != 1:
                    raise ModelError("模型必须返回恰好一个工具调用")
                return _parse_decision(calls[0])
            except Exception as exc:
                last_error = exc
        raise ModelError(f"模型动作解析失败：{type(last_error).__name__}") from last_error


class DeepSeekWriterModel:
    """DeepSeek 脚本 Writer。"""

    def __init__(self, settings: Settings) -> None:
        """输入：配置；输出：Writer；作用：绑定 submit_script 工具；实现：强制工具选择。"""
        self.settings = settings
        self._model = _make_model(settings)
        # deepseek-flash 的 thinking mode 不支持强制指定 function tool_choice；
        # 使用 auto，并在下方校验模型确实返回了 submit_script。
        self._writer = self._model.bind_tools(
            [_SUBMIT_SCRIPT_TOOL],
            tool_choice="auto",
        )

    async def write(self, context: WriterContext) -> Script:
        """生成或修订脚本。

        输入：Writer 上下文。
        输出：合法 `Script`；引用未知 claim 时拒绝。
        作用：把写作限制在事实白名单内。
        实现：读取 write/revise Prompt；重试一次；用 Pydantic 校验工具参数。
        """
        prompt_name = "revise.md" if context.previous_script is not None else "write.md"
        payload = writer_context_payload(context)
        prompt = _read_prompt(prompt_name).replace(
            "{context}", json.dumps(payload, ensure_ascii=False)[:16000]
        )
        allowed_claim_ids = {claim.claim_id for claim in context.claims if claim.approved}
        last_error: Exception | None = None
        for _ in range(2):
            try:
                response = await self._writer.ainvoke(
                    [SystemMessage(content=prompt), HumanMessage(content="请调用 submit_script。")]
                )
                calls = list(getattr(response, "tool_calls", []) or [])
                if len(calls) != 1 or calls[0].get("name") != "submit_script":
                    raise ModelError("Writer 必须返回一个 submit_script 调用")
                script = Script.model_validate(calls[0].get("args") or {})
                used = {claim_id for shot in script.shots for claim_id in shot.claim_ids}
                if not used.issubset(allowed_claim_ids):
                    raise ModelError("Writer 引用了不存在的 claim")
                return script
            except Exception as exc:
                last_error = exc
        raise ModelError(f"Writer 输出解析失败：{type(last_error).__name__}") from last_error
