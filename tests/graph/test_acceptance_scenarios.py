"""P0 五个验收场景（图 + 场景运行器）。"""

from __future__ import annotations

from src.domain.models import ActionDecision, Brief
from src.domain.status import ActionName
from src.runtime.scenarios import Assertions, ScenarioSpec, run_scenario
from tests.helpers import make_script, make_test_settings


def decisions(*actions: ActionName, args: list[dict] | None = None) -> list[ActionDecision]:
    """输入：动作序列；输出：带 reason 的决策；作用：压缩测试样板。"""
    result = []
    for index, action in enumerate(actions):
        payload = (args or [{}] * len(actions))[index]
        result.append(ActionDecision(action=action, args=payload, basis=f"test {action}"))
    return result


async def test_a_normal_generation() -> None:
    """A：38→28，规则通过，SUCCESS 且引用 P01。"""
    settings = make_test_settings()
    brief = Brief(topic="字幕", platform="抖音", target_duration_sec=30)
    queued = decisions(
        ActionName.SEARCH_MATERIALS,
        ActionName.DRAFT_SCRIPT,
        ActionName.ESTIMATE_DURATION,
        ActionName.REVISE_SCRIPT,
        ActionName.ESTIMATE_DURATION,
        ActionName.CHECK_RULES,
        ActionName.FINALIZE,
        args=[{"query": "字幕", "top_k": 5}, {}, {}, {}, {}, {}, {}],
    )
    scenario = ScenarioSpec(
        decisions=queued,
        scripts=[make_script("draft_01"), make_script("draft_02")],
        tool_overrides={
            "search_materials": [
                {"status": "OK", "results": [{"source_id": "P01", "text": "产品支持字幕功能"}]}
            ],
            "duration_total_sec": [38, 28],
        },
    )
    outcome = await run_scenario(
        settings,
        brief,
        scenario,
        Assertions(
            final_status="SUCCESS",
            must_include_source="P01",
            max_steps=8,
            must_not_include_text="效率提升 80%",
        ),
    )
    assert outcome.passed, outcome.assertions
    assert outcome.status == "SUCCESS"
    assert outcome.result is not None
    assert outcome.result["estimated_duration_sec"] == 28
    assert outcome.result["sources"][0]["source_id"] == "P01"


async def test_b_missing_information_waits() -> None:
    """B：缺平台/时长 → WAITING_INPUT，不猜测。"""
    outcome = await run_scenario(
        make_test_settings(),
        Brief(topic="字幕"),
        ScenarioSpec(decisions=[]),
        Assertions(final_status="WAITING_INPUT"),
    )
    assert outcome.status == "WAITING_INPUT"
    assert outcome.passed
    assert outcome.result is None


async def test_b_resume_continues_after_answer() -> None:
    """B 续：补充答案后继续，不重置流程。"""
    queued = decisions(
        ActionName.SEARCH_MATERIALS,
        ActionName.DRAFT_SCRIPT,
        ActionName.ESTIMATE_DURATION,
        ActionName.CHECK_RULES,
        ActionName.FINALIZE,
        args=[{"query": "字幕"}, {}, {}, {}, {}],
    )
    scenario = ScenarioSpec(
        decisions=queued,
        scripts=[make_script()],
        tool_overrides={
            "search_materials": [
                {"status": "OK", "results": [{"source_id": "P01", "text": "产品支持字幕功能"}]}
            ],
            "duration_total_sec": [28],
        },
        resume_answer={"platform": "抖音", "target_duration_sec": 30},
    )
    outcome = await run_scenario(
        make_test_settings(),
        Brief(topic="字幕"),
        scenario,
        Assertions(final_status="SUCCESS", must_include_source="P01"),
    )
    assert outcome.passed, outcome.assertions


async def test_c_no_material() -> None:
    """C：连续两次 EMPTY → NO_MATERIAL，无脚本。"""
    queued = decisions(
        ActionName.SEARCH_MATERIALS,
        ActionName.SEARCH_MATERIALS,
        ActionName.FAIL,
        args=[
            {"query": "字幕"},
            {"query": "加字幕"},
            {"status": "NO_MATERIAL", "message": "没有可用素材"},
        ],
    )
    scenario = ScenarioSpec(
        decisions=queued,
        tool_overrides={
            "search_materials": [
                {"status": "EMPTY", "results": []},
                {"status": "EMPTY", "results": []},
            ]
        },
    )
    outcome = await run_scenario(
        make_test_settings(),
        Brief(topic="字幕", platform="抖音", target_duration_sec=30),
        scenario,
        Assertions(final_status="NO_MATERIAL"),
    )
    assert outcome.status == "NO_MATERIAL"
    assert outcome.result is not None
    assert outcome.result["script"] is None
    assert outcome.passed


async def test_d_tool_error_is_not_faked() -> None:
    """D：检索 ERROR → TOOL_ERROR，安全护栏覆盖后续动作。"""
    queued = decisions(
        ActionName.SEARCH_MATERIALS,
        ActionName.FINALIZE,
        args=[{"query": "字幕"}, {}],
    )
    scenario = ScenarioSpec(
        decisions=queued,
        tool_overrides={"search_materials": [{"status": "ERROR", "error": "timeout"}]},
    )
    outcome = await run_scenario(
        make_test_settings(),
        Brief(topic="字幕", platform="抖音", target_duration_sec=30),
        scenario,
        Assertions(final_status="TOOL_ERROR"),
    )
    assert outcome.status == "TOOL_ERROR"
    assert outcome.passed


async def test_e_unsatisfiable_duration() -> None:
    """E：22→20→18，目标 15，修订耗尽 → UNSATISFIABLE_DURATION。"""
    queued = decisions(
        ActionName.SEARCH_MATERIALS,
        ActionName.DRAFT_SCRIPT,
        ActionName.ESTIMATE_DURATION,
        ActionName.REVISE_SCRIPT,
        ActionName.ESTIMATE_DURATION,
        ActionName.REVISE_SCRIPT,
        ActionName.ESTIMATE_DURATION,
        ActionName.REVISE_SCRIPT,
        args=[
            {"query": "字幕"},
            {},
            {},
            {},
            {},
            {},
            {},
            {},
        ],
    )
    scenario = ScenarioSpec(
        decisions=queued,
        scripts=[make_script("draft_01"), make_script("draft_02"), make_script("draft_03")],
        tool_overrides={
            "search_materials": [
                {"status": "OK", "results": [{"source_id": "P01", "text": "产品支持字幕功能"}]}
            ],
            "duration_total_sec": [22, 20, 18],
        },
    )
    outcome = await run_scenario(
        make_test_settings(),
        Brief(topic="字幕", platform="抖音", target_duration_sec=15),
        scenario,
        Assertions(final_status="UNSATISFIABLE_DURATION"),
    )
    assert outcome.status == "UNSATISFIABLE_DURATION"
    assert outcome.passed
    assert outcome.result is not None
    assert outcome.result["attempts"]["revisions"] == 2
    assert outcome.result["estimated_duration_sec"] == 18
    assert outcome.result["suggestions"]
