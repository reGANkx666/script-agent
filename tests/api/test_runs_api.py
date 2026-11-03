"""API 测试：创建、轮询、结果、Trace、SSE、恢复与取消。"""

from __future__ import annotations

import asyncio

import httpx

from src.api.app import create_app
from src.domain.models import ActionDecision
from src.domain.status import ActionName, TaskStatus
from tests.helpers import (
    make_container,
    make_script,
    make_services,
    success_search_result,
    make_test_settings,
)

TERMINAL = {
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
}


def _decisions(actions: list[tuple[ActionName, dict]]) -> list[ActionDecision]:
    """输入：动作与参数；输出：决策队列；作用：压缩 API 测试数据。"""
    return [ActionDecision(action=action, args=args, basis="api test") for action, args in actions]


def _client(container) -> httpx.AsyncClient:
    """输入：容器；输出：ASGI 客户端；作用：不依赖真实端口。"""
    app = create_app(container.settings, container=container)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _wait(client: httpx.AsyncClient, run_id: str, expected: str, timeout: float = 5.0):
    """输入：客户端/run/期望状态；输出：状态 JSON；作用：轮询到目标终态。"""
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/v1/runs/{run_id}")
        last = response.json()
        if last.get("status") == expected:
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"run status {last} != {expected}")


async def _ready_success_container():
    """输出：完整成功流程容器；作用：复用测试数据。"""
    settings = make_test_settings()
    services = make_services(
        settings,
        decisions=_decisions(
            [
                (ActionName.SEARCH_MATERIALS, {"query": "字幕"}),
                (ActionName.DRAFT_SCRIPT, {}),
                (ActionName.ESTIMATE_DURATION, {}),
                (ActionName.REVISE_SCRIPT, {}),
                (ActionName.ESTIMATE_DURATION, {}),
                (ActionName.CHECK_RULES, {}),
                (ActionName.FINALIZE, {}),
            ]
        ),
        scripts=[make_script("draft_01"), make_script("draft_02")],
        search_results=[success_search_result()],
        duration_totals=[38, 28],
    )
    return make_container(services)


async def test_health_and_ready_for_test_container() -> None:
    """输入：Mock 容器；输出：healthz/readyz 200，凭据缺失不阻断测试。"""
    async with _client(await _ready_success_container()) as client:
        health = await client.get("/healthz")
        ready = await client.get("/readyz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert ready.status_code == 200
    assert ready.json()["ready"] is True


async def test_create_poll_result_trace_sse() -> None:
    """输入：完整运行；输出：202→SUCCESS→result/trace/SSE 可用。"""
    async with _client(await _ready_success_container()) as client:
        created = await client.post(
            "/v1/runs",
            json={
                "brief": {
                    "topic": "字幕",
                    "platform": "抖音",
                    "target_duration_sec": 30,
                    "audience": "新手",
                    "style": "轻快",
                }
            },
        )
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        status = await _wait(client, run_id, TaskStatus.SUCCESS.value)
        assert status["current_node"] == "finalize"
        result = await client.get(f"/v1/runs/{run_id}/result")
        assert result.status_code == 200
        assert result.json()["sources"][0]["source_id"] == "P01"
        assert result.json()["estimated_duration_sec"] == 28
        trace = await client.get(f"/v1/runs/{run_id}/trace")
        assert trace.status_code == 200
        assert any(item["event_type"] == "action.selected" for item in trace.json()["trace"])
        sse = await client.get(f"/v1/runs/{run_id}/events")
        assert sse.status_code == 200
        assert "event: node.started" in sse.text
        assert "event: run.completed" in sse.text


async def test_resume_only_waiting_and_continues() -> None:
    """输入：缺必填字段；输出：WAITING_INPUT→resume→SUCCESS；重复恢复 409。"""
    settings = make_test_settings()
    services = make_services(
        settings,
        decisions=_decisions(
            [
                (ActionName.SEARCH_MATERIALS, {"query": "字幕"}),
                (ActionName.DRAFT_SCRIPT, {}),
                (ActionName.ESTIMATE_DURATION, {}),
                (ActionName.CHECK_RULES, {}),
                (ActionName.FINALIZE, {}),
            ]
        ),
        scripts=[make_script()],
        search_results=[success_search_result()],
        duration_totals=[28],
    )
    async with _client(make_container(services)) as client:
        created = await client.post("/v1/runs", json={"brief": {"topic": "字幕"}})
        run_id = created.json()["run_id"]
        status = await _wait(client, run_id, TaskStatus.WAITING_INPUT.value)
        assert set(status["missing_fields"]) == {"platform", "target_duration_sec"}
        resumed = await client.post(
            f"/v1/runs/{run_id}/resume",
            json={"answer": {"platform": "抖音", "target_duration_sec": 30}},
        )
        assert resumed.status_code == 202
        await _wait(client, run_id, TaskStatus.SUCCESS.value)
        conflict = await client.post(
            f"/v1/runs/{run_id}/resume",
            json={"answer": {"platform": "抖音", "target_duration_sec": 30}},
        )
        assert conflict.status_code == 409


async def test_cancel_waiting_run() -> None:
    """输入：WAITING_INPUT；输出：cancel→CANCELLED，resume→409。"""
    async with _client(make_container(make_services(make_test_settings()))) as client:
        created = await client.post("/v1/runs", json={"brief": {"topic": "字幕"}})
        run_id = created.json()["run_id"]
        await _wait(client, run_id, TaskStatus.WAITING_INPUT.value)
        cancelled = await client.post(f"/v1/runs/{run_id}/cancel")
        assert cancelled.status_code == 202
        assert cancelled.json()["status"] == "CANCELLED"
        status = await client.get(f"/v1/runs/{run_id}")
        assert status.json()["status"] == "CANCELLED"
        resumed = await client.post(
            f"/v1/runs/{run_id}/resume",
            json={"answer": {"platform": "抖音", "target_duration_sec": 30}},
        )
        assert resumed.status_code == 409


async def test_test_api_runs_synchronously() -> None:
    """输入：Mock 场景；输出：同步结果 + 断言明细。"""
    async with _client(make_container(make_services(make_test_settings()))) as client:
        response = await client.post(
            "/v1/test/runs",
            json={
                "mode": "mock",
                "brief": {
                    "topic": "字幕",
                    "platform": "抖音",
                    "target_duration_sec": 30,
                },
                "scenario": {
                    "decisions": [
                        {
                            "action": "fail",
                            "args": {
                                "status": "NO_MATERIAL",
                                "message": "没有素材",
                            },
                            "basis": "test",
                        }
                    ]
                },
                "assertions": {"final_status": "NO_MATERIAL", "max_steps": 8},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "NO_MATERIAL"
        assert body["passed"] is True


async def test_not_found_and_invalid_resume() -> None:
    """输入：不存在的 run / 非等待态恢复；输出：404 / 409。"""
    async with _client(make_container(make_services(make_test_settings()))) as client:
        missing = await client.get("/v1/runs/does-not-exist")
        assert missing.status_code == 404
        created = await client.post("/v1/runs", json={"brief": {"topic": "字幕"}})
        run_id = created.json()["run_id"]
        # 创建后尚未进入 WAITING_INPUT 时恢复应被拒绝（或 404 仅在记录缺失时）。
        state = (await client.get(f"/v1/runs/{run_id}")).json()["status"]
        if state != "WAITING_INPUT":
            response = await client.post(
                f"/v1/runs/{run_id}/resume",
                json={"answer": {"platform": "抖音"}},
            )
            assert response.status_code == 409
