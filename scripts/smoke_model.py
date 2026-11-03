"""真实模型冒烟脚本。

输入：DeepSeek / DashScope 配置。
输出：一个合法工具动作 + 1024 维 embedding。
作用：验证外部凭据与协议；失败只报告，不伪装成 Mock 测试成功。
实现：DecisionModel + embedding 维度探测；不写数据库。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import get_settings  # noqa: E402
from src.domain.models import Brief  # noqa: E402
from src.llm.base import DecisionContext, ModelError  # noqa: E402
from src.llm.deepseek import DeepSeekDecisionModel  # noqa: E402
from src.tools.embeddings import DashScopeEmbedder, verify_embedding_dimension  # noqa: E402


async def main() -> int:
    """执行冒烟。

    输入：环境配置。
    输出：进程码；任一失败返回 1。
    作用：人工部署前检查。
    实现：真实调用再断言协议形状；不记录 Key。
    """
    settings = get_settings()
    if settings.config_errors(require_credentials=True):
        print("ERROR: " + "; ".join(settings.config_errors(require_credentials=True)))
        return 1
    try:
        model = DeepSeekDecisionModel(settings)
        decision = await model.decide(
            DecisionContext(
                brief=Brief(
                    topic="新手如何添加字幕",
                    platform="抖音",
                    target_duration_sec=30,
                ),
                previous_queries=[],
            )
        )
        print(f"OK: deepseek_action={decision.action} basis={decision.basis[:80]}")
    except (ModelError, Exception) as exc:
        print(f"ERROR: DeepSeek 冒烟失败：{type(exc).__name__}")
        return 1

    try:
        embedder = DashScopeEmbedder(settings)
        await verify_embedding_dimension(embedder, settings.embedding_dim)
        print(f"OK: dashscope_embedding_dim={settings.embedding_dim}")
    except Exception as exc:
        print(f"ERROR: DashScope 冒烟失败：{type(exc).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
