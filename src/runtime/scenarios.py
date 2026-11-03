"""同步测试场景执行器。

输入：动作队列、工具覆盖、可选脚本与断言。
输出：终态、结果、事件和 pass/fail。
作用：让 P0 五个验收场景不依赖真实模型、数据库或网络。
实现：MemorySaver + FakeServices + 同步 `ainvoke`；不写生产表。
"""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.types import Command
from pydantic import BaseModel, Field

from src.config import Settings
from src.domain.duration_policy import report_with_total
from src.domain.models import (
    ActionDecision,
    Brief,
    RuleSet,
    Script,
    SearchHit,
    SearchResult,
)
from src.graph.build import build_graph
from src.graph.services import GraphServices
from src.graph.state import initial_state
from src.llm.mock import MockDecisionModel, ScriptedWriterModel, TemplateWriterModel
from src.runtime.assembler import interrupt_payloads
from src.runtime.checkpointer import make_memory_saver
from src.tools.duration import estimate_duration_tool
from src.tools.retrieval import StaticRetrievalTool
from src.tools.rule_repository import StaticRuleProvider, default_rule_provider


class Assertions(BaseModel):
    """测试断言。"""

    final_status: str | None = None
    must_include_source: str | None = None
    max_steps: int | None = None
    must_not_include_text: str | None = None


class ScenarioSpec(BaseModel):
    """可复现场景。"""

    decisions: list[ActionDecision] = Field(default_factory=list)
    scripts: list[Script] = Field(default_factory=list)
    tool_overrides: dict[str, Any] = Field(default_factory=dict)
    resume_answer: dict[str, Any] | None = None
    rules: RuleSet | None = None


class ScenarioOutcome(BaseModel):
    """同步场景返回。"""

    run_id: str
    passed: bool
    status: str
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)
    assertions: dict[str, Any] = Field(default_factory=dict)


async def run_scenario(
    settings: Settings,
    brief: Brief,
    scenario: ScenarioSpec,
    assertions: Assertions | None = None,
    run_id: str | None = None,
) -> ScenarioOutcome:
    """同步执行一个测试场景。

    输入：配置、Brief、场景、断言、可选 run_id。
    输出：`ScenarioOutcome`。
    作用：测试 API 与图集成测试共用同一条执行路径。
    实现：构建 FakeServices → MemorySaver 图 → 处理 interrupt/resume → 断言。
    """
    run_id = run_id or str(uuid.uuid4())
    assertions = assertions or Assertions()
    events: list[dict[str, Any]] = []
    seq = 0

    async def sink(event: dict[str, Any]) -> None:
        nonlocal seq
        seq += 1
        events.append(
            {
                "seq": seq,
                "event_type": event.get("event_type"),
                "node": event.get("node"),
                "payload": event.get("payload") or {},
            }
        )

    services = build_scenario_services(settings, brief, scenario)
    graph = build_graph(services, make_memory_saver())
    config = {
        "configurable": {"thread_id": run_id, "event_sink": sink},
        "recursion_limit": settings.recursion_limit,
    }
    status = "INTERRUPTED"
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    values: dict[str, Any] = {}
    try:
        await graph.ainvoke(initial_state(run_id, brief), config)
        snapshot = await graph.aget_state(config)
        waiting = interrupt_payloads(snapshot)
        if waiting:
            seq += 1
            events.append(
                {
                    "seq": seq,
                    "event_type": "run.waiting_input",
                    "node": "ask_user",
                    "payload": waiting[0],
                }
            )
        if waiting and scenario.resume_answer is not None:
            await graph.ainvoke(Command(resume=scenario.resume_answer), config)
            snapshot = await graph.aget_state(config)
            waiting = interrupt_payloads(snapshot)
            if waiting:
                seq += 1
                events.append(
                    {
                        "seq": seq,
                        "event_type": "run.waiting_input",
                        "node": "ask_user",
                        "payload": waiting[0],
                    }
                )
        values = snapshot.values or {}
        if waiting:
            status = "WAITING_INPUT"
            error = {"status": status, "message": waiting[0].get("question", "等待用户补充")}
        else:
            status = str(values.get("status") or "INTERRUPTED")
            result = values.get("result")
            error = values.get("error")
            if status not in {
                "SUCCESS",
                "NEEDS_INPUT",
                "NO_MATERIAL",
                "TOOL_ERROR",
                "UNSATISFIABLE_DURATION",
                "UNSATISFIABLE_RULES",
                "MAX_STEPS",
                "MODEL_ERROR",
                "CANCELLED",
                "INTERRUPTED",
            }:
                status = "INTERRUPTED"
    except Exception as exc:
        status = "INTERRUPTED"
        error = {"status": status, "message": f"场景执行异常：{type(exc).__name__}"}

    passed, assertion_report = evaluate_assertions(status, result, values, assertions)
    return ScenarioOutcome(
        run_id=run_id,
        passed=passed,
        status=status,
        result=result,
        error=error,
        trace=events,
        assertions=assertion_report,
    )


def build_scenario_services(
    settings: Settings, brief: Brief, scenario: ScenarioSpec
) -> GraphServices:
    """输入：配置/Brief/场景；输出：FakeServices。

    作用：将 JSON 场景映射为确定性工具替身。
    实现：动作/脚本队列；检索结果覆盖；时长总秒数覆盖；规则覆盖。
    """
    writer = (
        ScriptedWriterModel(scenario.scripts)
        if scenario.scripts
        else TemplateWriterModel(target_sec=brief.target_duration_sec or 30)
    )
    retrieval = StaticRetrievalTool(
        _parse_search_overrides(scenario.tool_overrides.get("search_materials"))
    )
    duration_estimator = _make_duration_estimator(
        scenario.tool_overrides.get("duration_total_sec")
    )
    if scenario.rules is not None:
        rule_provider = StaticRuleProvider(scenario.rules.brand, scenario.rules.platform)
    else:
        rule_provider = default_rule_provider()
    return GraphServices(
        settings=settings,
        decision_model=MockDecisionModel(scenario.decisions),
        writer_model=writer,
        retrieval=retrieval,
        rule_provider=rule_provider,
        duration_estimator=duration_estimator,
    )


def _parse_search_overrides(raw: Any) -> list[SearchResult]:
    """输入：场景覆盖；输出：检索结果队列；作用：兼容计划中的精简 schema。

    实现：接受单对象或列表；每条结果补默认 score/source。
    """
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    parsed: list[SearchResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        hits = []
        for index, hit in enumerate(item.get("results") or [], start=1):
            if not isinstance(hit, dict) or not hit.get("source_id"):
                continue
            hits.append(
                SearchHit(
                    source_id=str(hit["source_id"]),
                    text=str(hit.get("text", "")),
                    score=float(hit.get("score", 1.0 / index)),
                    source=str(hit.get("source", "test")),
                    source_type=str(hit.get("source_type", "product_brief")),
                    matched_terms=list(hit.get("matched_terms") or []),
                )
            )
        parsed.append(
            SearchResult(
                status=str(item.get("status", "OK")),
                query=str(item.get("query", "")),
                results=hits,
                debug=dict(item.get("debug") or {}),
                error=item.get("error"),
            )
        )
    return parsed


def _make_duration_estimator(raw: Any):
    """输入：duration_total_sec 覆盖；输出：估算器；作用：稳定复现 38/28/18 边界。"""
    totals = [int(item) for item in raw] if isinstance(raw, list) else []
    cursor = {"index": 0}

    def estimator(script: Script, target_sec: int, tolerance_sec: int):
        """输入：脚本/目标/容差；输出：覆盖或真实估算；实现：顺序消费覆盖值。"""
        if cursor["index"] < len(totals):
            total = totals[cursor["index"]]
            cursor["index"] += 1
            return report_with_total(script, total, target_sec, tolerance_sec)
        return estimate_duration_tool(script, target_sec, tolerance_sec)

    return estimator


def evaluate_assertions(
    status: str,
    result: dict[str, Any] | None,
    final_state: dict[str, Any],
    assertions: Assertions,
) -> tuple[bool, dict[str, Any]]:
    """输入：终态/结果/断言；输出：(是否通过, 明细)。

    作用：测试 API 返回可解释的 pass/fail。
    实现：逐项检查，缺失的断言不参与判断。
    """
    checks: dict[str, Any] = {}
    if assertions.final_status is not None:
        checks["final_status"] = {
            "expected": assertions.final_status,
            "actual": status,
            "passed": status == assertions.final_status,
        }
    if assertions.must_include_source is not None:
        source_ids = [item.get("source_id") for item in (result or {}).get("sources", [])]
        checks["must_include_source"] = {
            "expected": assertions.must_include_source,
            "actual": source_ids,
            "passed": assertions.must_include_source in source_ids,
        }
    if assertions.max_steps is not None:
        steps = int(final_state.get("step", 0))
        checks["max_steps"] = {
            "expected": assertions.max_steps,
            "actual": steps,
            "passed": steps <= assertions.max_steps,
        }
    if assertions.must_not_include_text is not None:
        needle = assertions.must_not_include_text
        script = (result or {}).get("script") or {}
        present = needle in str(script)
        checks["must_not_include_text"] = {
            "expected_absent": needle,
            "passed": not present,
        }
    passed = all(item["passed"] for item in checks.values())
    return passed, checks
