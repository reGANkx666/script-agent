"""复现 6 个验收场景并导出 Trace。

输入：`examples/scenarios/*.json`。
输出：`examples/traces/*.jsonl` 与 `examples/results/*.json`。
作用：让验收结果可离线复算，不依赖真实模型/数据库。
实现：加载场景 → `run_scenario` → 校验断言 → 写 JSONL。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import Settings  # noqa: E402
from src.domain.models import Brief  # noqa: E402
from src.observability.trace import build_trace  # noqa: E402
from src.runtime.scenarios import Assertions, ScenarioSpec, run_scenario  # noqa: E402
from src.runtime.store_types import EventRecord  # noqa: E402


async def main() -> int:
    """执行全部示例。

    输入：示例文件。
    输出：进程码；0 表示所有断言通过。
    作用：CI/人工复现。
    实现：逐个场景运行并落盘；任何断言失败返回 1。
    """
    scenario_dir = ROOT / "examples" / "scenarios"
    trace_dir = ROOT / "examples" / "traces"
    result_dir = ROOT / "examples" / "results"
    trace_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    for path in sorted(scenario_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        name = payload["name"]
        settings = Settings(_env_file=None, **(payload.get("settings") or {}))
        outcome = await run_scenario(
            settings=settings,
            brief=Brief.model_validate(payload["brief"]),
            scenario=ScenarioSpec.model_validate(payload["scenario"]),
            assertions=Assertions.model_validate(payload.get("assertions", {})),
            run_id=f"example-{name}",
        )
        projected = build_trace(
            run_id=f"example-{name}",
            events=[
                EventRecord(
                    id=index,
                    run_id=f"example-{name}",
                    seq=int(event["seq"]),
                    event_type=str(event["event_type"]),
                    node=event.get("node"),
                    payload=dict(event.get("payload") or {}),
                )
                for index, event in enumerate(outcome.trace, start=1)
            ],
        )
        with (trace_dir / f"{name}.jsonl").open("w", encoding="utf-8") as handle:
            for line in projected:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        (result_dir / f"{name}.json").write_text(
            json.dumps(outcome.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        marker = "PASS" if outcome.passed else "FAIL"
        print(f"{marker}: {name} status={outcome.status}")
        if not outcome.passed:
            failures.append(name)

    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print(f"OK: {len(list(scenario_dir.glob('*.json')))} scenarios reproduced")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
