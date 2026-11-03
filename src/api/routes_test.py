"""同步测试场景 API。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import get_container
from src.api.schemas import TestRunRequest
from src.runtime.container import AppContainer
from src.runtime.scenarios import ScenarioOutcome, run_scenario

router = APIRouter(prefix="/v1/test", tags=["test"])


@router.post("/runs", response_model=ScenarioOutcome)
async def run_test_scenario(
    payload: TestRunRequest,
    container: AppContainer = Depends(get_container),
) -> ScenarioOutcome:
    """同步执行 Mock 验收场景。

    输入：Brief、动作队列、工具覆盖、断言。
    输出：终态、结果、Trace 和断言明细。
    作用：CI/验收不依赖真实模型与生产库。
    实现：`ENABLE_TEST_API` 关闭时返回 404；使用 MemorySaver + FakeServices。
    """
    if not container.settings.enable_test_api:
        raise HTTPException(status_code=404, detail="测试接口已关闭")
    return await run_scenario(
        settings=container.settings,
        brief=payload.brief,
        scenario=payload.scenario,
        assertions=payload.assertions,
    )
