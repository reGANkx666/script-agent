"""可选 PostgreSQL 集成测试。

输入：`TEST_DATABASE_URL`。
输出：pgvector 与核心表存在性断言。
作用：提供真实库验收入口，同时在普通 CI 中跳过。
实现：只读查询；不建库、不写生产数据。
"""

from __future__ import annotations

import os

import psycopg
import pytest

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="未配置 TEST_DATABASE_URL")
def test_pgvector_and_required_tables() -> None:
    """输入：TEST_DATABASE_URL；输出：扩展与表存在；作用：真实库冒烟。"""
    with psycopg.connect(TEST_DATABASE_URL, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
            row = cur.fetchone()
            assert row, "pgvector 未启用"
            cur.execute(
                """
                SELECT format_type(a.atttypid, a.atttypmod)
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname='public' AND c.relname='materials'
                  AND a.attname='embedding' AND a.attnum > 0
                """
            )
            column = cur.fetchone()
            assert column and "vector(1024)" in column[0]
            for table in (
                "materials",
                "brand_rules",
                "platform_rules",
                "runs",
                "events",
                "knowledge_documents",
                "knowledge_chunks",
                "knowledge_ingestion_jobs",
            ):
                cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
                assert cur.fetchone()[0] is not None
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name='materials'
                  AND column_name IN ('document_id','chunk_id','parent_id','content_hash')
                """
            )
            columns = {row[0] for row in cur.fetchall()}
            assert columns == {"document_id", "chunk_id", "parent_id", "content_hash"}
