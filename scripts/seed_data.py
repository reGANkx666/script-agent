"""种子数据脚本。

输入：`data/materials.json`、`data/rules.json`、DashScope 配置。
输出：materials / brand_rules / platform_rules upsert。
作用：构造可复现的 P0 素材与规则。
实现：真实 embedding + psycopg 原生 SQL；重复执行幂等；不提交真实敏感资料。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import get_settings  # noqa: E402
from src.tools.embeddings import DashScopeEmbedder  # noqa: E402

DATA_DIR = ROOT / "data"


def _vector_literal(vector: list[float]) -> str:
    """输入：向量；输出：pgvector 字面量；作用：参数化写入；实现：方括号拼接。"""
    return "[" + ",".join(f"{value:.8f}" for value in vector) + "]"


def main() -> int:
    """执行种子写入。

    输入：JSON 数据与配置。
    输出：进程码。
    作用：首次部署/重置测试环境。
    实现：embedding 维度断言 → upsert 素材 → upsert 规则 → 打印统计。
    """
    settings = get_settings()
    if not settings.database_url:
        print("ERROR: DATABASE_URL 未配置")
        return 1
    if not settings.dashscope_api_key:
        print("ERROR: DASHSCOPE_API_KEY 未配置，无法生成 embedding")
        return 1
    materials = json.loads((DATA_DIR / "materials.json").read_text(encoding="utf-8"))
    rules = json.loads((DATA_DIR / "rules.json").read_text(encoding="utf-8"))
    embedder = DashScopeEmbedder(settings)
    vectors = embedder.embed_documents([item["text"] for item in materials])
    if any(len(vector) != settings.embedding_dim for vector in vectors):
        print("ERROR: embedding 维度与 EMBEDDING_DIM 不一致")
        return 1

    try:
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                for item, vector in zip(materials, vectors, strict=True):
                    cur.execute(
                        """
                        INSERT INTO materials
                          (id, text, source, source_type, scope, tags, active, embedding, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector,now())
                        ON CONFLICT (id) DO UPDATE SET
                          text=EXCLUDED.text, source=EXCLUDED.source,
                          source_type=EXCLUDED.source_type, scope=EXCLUDED.scope,
                          tags=EXCLUDED.tags, active=EXCLUDED.active,
                          embedding=EXCLUDED.embedding, updated_at=now()
                        """,
                        (
                            item["id"],
                            item["text"],
                            item["source"],
                            item.get("source_type", "product_brief"),
                            item.get("scope"),
                            item.get("tags", []),
                            item.get("active", True),
                            _vector_literal(vector),
                        ),
                    )
                for item in rules.get("brand", []):
                    cur.execute(
                        """
                        INSERT INTO brand_rules (id, rule, severity, active, version, updated_at)
                        VALUES (%s,%s,%s,true,'1',now())
                        ON CONFLICT (id) DO UPDATE SET
                          rule=EXCLUDED.rule, severity=EXCLUDED.severity,
                          active=true, updated_at=now()
                        """,
                        (item["id"], Jsonb(item["rule"]), item["severity"]),
                    )
                for platform, items in rules.get("platform", {}).items():
                    for item in items:
                        cur.execute(
                            """
                            INSERT INTO platform_rules
                              (platform, code, rule, severity, active, updated_at)
                            VALUES (%s,%s,%s,%s,true,now())
                            ON CONFLICT (platform, code) DO UPDATE SET
                              rule=EXCLUDED.rule, severity=EXCLUDED.severity,
                              active=true, updated_at=now()
                            """,
                            (platform, item["code"], Jsonb(item["rule"]), item.get("severity", "block")),
                        )
        print(
            f"OK: materials={len(materials)}, brand_rules={len(rules.get('brand', []))}, "
            f"platforms={len(rules.get('platform', {}))}, embedding_dim={settings.embedding_dim}"
        )
        return 0
    except Exception as exc:
        print(f"ERROR: 写入失败：{type(exc).__name__}（不输出连接串或凭据）")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
