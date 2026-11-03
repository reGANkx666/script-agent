"""测试辅助：构造确定性服务与内存容器。"""

from __future__ import annotations

from typing import Any

from src.config import Settings
from src.domain.duration_policy import report_with_total
from src.domain.models import ActionDecision, Script, SearchResult
from src.graph.build import build_graph
from src.graph.services import GraphServices
from src.llm.mock import MockDecisionModel, ScriptedWriterModel, TemplateWriterModel
from src.runtime.checkpointer import make_memory_saver
from src.runtime.container import AppContainer
from src.runtime.db import ReadinessState
from src.runtime.event_store import MemoryEventStore
from src.runtime.run_manager import RunManager
from src.runtime.run_store import MemoryRunStore
from src.tools.duration import estimate_duration_tool
from src.tools.retrieval import StaticRetrievalTool
from src.tools.rule_repository import RuleProvider, default_rule_provider


def make_test_settings(**overrides: Any) -> Settings:
    """输出：不读取本机 .env 的测试配置；作用：隔离测试；实现：`_env_file=None`。"""
    return Settings(_env_file=None, enable_test_api=True, **overrides)


def make_script(
    script_id: str = "draft_01",
    narration: str = "产品支持字幕功能",
    claim_ids: list[str] | None = None,
    shot_id: str = "S01",
    visual_hold_sec: int = 0,
) -> Script:
    """输入：可选脚本参数；输出：合法脚本；作用：减少测试样板。"""
    return Script(
        script_id=script_id,
        title="字幕入门",
        shots=[
            {
                "shot_id": shot_id,
                "visual": "产品界面使用画面",
                "narration": narration,
                "subtitle": "字幕功能",
                "visual_hold_sec": visual_hold_sec,
                "claim_ids": ["C-P01"] if claim_ids is None else claim_ids,
            }
        ],
    )


def make_services(
    settings: Settings,
    *,
    decisions: list[ActionDecision] | None = None,
    scripts: list[Script] | None = None,
    search_results: list[SearchResult] | None = None,
    duration_totals: list[int] | None = None,
    rule_provider: RuleProvider | None = None,
) -> GraphServices:
    """输入：测试配置与替身队列；输出：GraphServices；作用：固定图执行。"""
    writer = (
        ScriptedWriterModel(scripts)
        if scripts
        else TemplateWriterModel(target_sec=30)
    )
    totals = list(duration_totals or [])
    duration_index = {"value": 0}

    def duration_estimator(script: Script, target_sec: int, tolerance_sec: int):
        """输入：脚本；输出：覆盖/真实时长；实现：顺序消费测试覆盖。"""
        if duration_index["value"] < len(totals):
            total = totals[duration_index["value"]]
            duration_index["value"] += 1
            return report_with_total(script, total, target_sec, tolerance_sec)
        return estimate_duration_tool(script, target_sec, tolerance_sec)

    return GraphServices(
        settings=settings,
        decision_model=MockDecisionModel(list(decisions or [])),
        writer_model=writer,
        retrieval=StaticRetrievalTool(list(search_results or [])),
        rule_provider=rule_provider or default_rule_provider(),
        duration_estimator=duration_estimator,
    )


def make_container(services: GraphServices) -> AppContainer:
    """输入：图依赖；输出：ready 的内存容器；作用：API 测试不连数据库。"""
    settings = services.settings
    graph = build_graph(services, make_memory_saver())
    run_store = MemoryRunStore()
    event_store = MemoryEventStore()
    manager = RunManager(settings, graph, run_store, event_store)
    return AppContainer(
        settings=settings,
        ready=True,
        readiness=ReadinessState(ready=True, details={"checkpointer": "memory"}),
        run_store=run_store,
        event_store=event_store,
        run_manager=manager,
        graph=graph,
        test_mode=True,
    )


def success_search_result() -> SearchResult:
    """输出：包含 P01 的检索结果；作用：验收场景 A。"""
    from src.domain.models import SearchHit

    return SearchResult(
        status="OK",
        query="字幕",
        results=[
            SearchHit(
                source_id="P01",
                text="产品支持字幕功能",
                source="产品说明 v1.2",
                score=1.0,
            )
        ],
    )
