"""数据库迁移脚本。

输入：`DATABASE_URL`。
输出：当前库内的 pgvector 扩展、表、索引与维度检查。
作用：不创建数据库，只初始化本项目 schema。
实现：psycopg 原生 SQL；无权限或版本不满足时明确失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import get_settings  # noqa: E402

DDL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS materials (
  id            text PRIMARY KEY,
  text          text NOT NULL,
  source        text NOT NULL,
  source_type   text NOT NULL DEFAULT 'product_brief',
  scope         text,
  tags          text[] NOT NULL DEFAULT '{}',
  active        boolean NOT NULL DEFAULT true,
  embedding     vector(1024),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS materials_active_idx ON materials (active);
CREATE INDEX IF NOT EXISTS materials_embedding_hnsw
  ON materials USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS brand_rules (
  id          text PRIMARY KEY,
  rule        jsonb NOT NULL,
  severity    text NOT NULL,
  active      boolean NOT NULL DEFAULT true,
  version     text NOT NULL DEFAULT '1',
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS platform_rules (
  platform    text NOT NULL,
  code        text NOT NULL,
  rule        jsonb NOT NULL,
  severity    text NOT NULL DEFAULT 'block',
  active      boolean NOT NULL DEFAULT true,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (platform, code)
);

-- 兼容已创建过的旧表（migrate 是幂等的，不依赖表重建）
ALTER TABLE platform_rules
  ADD COLUMN IF NOT EXISTS severity text NOT NULL DEFAULT 'block';

CREATE TABLE IF NOT EXISTS runs (
  run_id        uuid PRIMARY KEY,
  status        text NOT NULL,
  request       jsonb NOT NULL,
  result        jsonb,
  error         jsonb,
  current_node  text,
  model         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
  id          bigserial PRIMARY KEY,
  run_id      uuid NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  seq         integer NOT NULL,
  event_type  text NOT NULL,
  node        text,
  payload     jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (run_id, seq)
);

CREATE INDEX IF NOT EXISTS events_run_seq_idx ON events (run_id, seq);

CREATE TABLE IF NOT EXISTS knowledge_documents (
  id                uuid PRIMARY KEY,
  filename          text NOT NULL,
  mime              text NOT NULL DEFAULT '',
  size_bytes        bigint NOT NULL DEFAULT 0,
  content_hash      text NOT NULL UNIQUE,
  status            text NOT NULL,
  error             text,
  chunker_version   text NOT NULL DEFAULT 'pc-v1',
  embedding_model   text NOT NULL DEFAULT '',
  title             text,
  storage_path      text,
  active            boolean NOT NULL DEFAULT false,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS knowledge_documents_active_idx
  ON knowledge_documents (active, created_at DESC);

ALTER TABLE knowledge_documents
  ADD COLUMN IF NOT EXISTS storage_path text;

CREATE TABLE IF NOT EXISTS knowledge_chunks (
  id            text PRIMARY KEY,
  document_id   uuid NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
  parent_id     text REFERENCES knowledge_chunks(id) ON DELETE CASCADE,
  chunk_type    text NOT NULL CHECK (chunk_type IN ('parent','child')),
  ordinal       integer NOT NULL,
  heading_path  text[] NOT NULL DEFAULT '{}',
  text          text NOT NULL,
  token_count   integer NOT NULL DEFAULT 0,
  embedding     vector(1024),
  metadata      jsonb NOT NULL DEFAULT '{}',
  active        boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_id, ordinal)
);

CREATE INDEX IF NOT EXISTS knowledge_chunks_document_idx
  ON knowledge_chunks (document_id, chunk_type, ordinal);
CREATE INDEX IF NOT EXISTS knowledge_chunks_parent_idx
  ON knowledge_chunks (parent_id);
CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_hnsw
  ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS knowledge_ingestion_jobs (
  id            uuid PRIMARY KEY,
  document_id   uuid NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
  status        text NOT NULL,
  stage         text NOT NULL DEFAULT 'QUEUED',
  progress      integer NOT NULL DEFAULT 0,
  error         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS knowledge_jobs_document_idx
  ON knowledge_ingestion_jobs (document_id, created_at DESC);

ALTER TABLE materials
  ADD COLUMN IF NOT EXISTS document_id uuid,
  ADD COLUMN IF NOT EXISTS chunk_id text,
  ADD COLUMN IF NOT EXISTS parent_id text,
  ADD COLUMN IF NOT EXISTS content_hash text;

CREATE INDEX IF NOT EXISTS materials_document_idx
  ON materials (document_id);
"""


def main() -> int:
    """执行迁移。

    输入：环境配置。
    输出：进程码；0 成功，1 明确失败。
    作用：DBA/开发在目标库执行；不自动 CREATE DATABASE。
    实现：单连接事务执行 DDL；随后查询扩展与 embedding 类型。
    """
    settings = get_settings()
    if not settings.database_url:
        print("ERROR: DATABASE_URL 未配置")
        return 1
    try:
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(DDL)
                cur.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
                row = cur.fetchone()
                if not row:
                    print("ERROR: pgvector 未启用，请让 DBA 安装扩展")
                    return 1
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
                embedding = cur.fetchone()
                column_type = str(embedding[0]) if embedding else ""
                if "vector(1024)" not in column_type:
                    print(f"ERROR: materials.embedding 类型不匹配：{column_type}")
                    return 1
        print(f"OK: pgvector={row[0]}, materials.embedding={column_type}")
        return 0
    except Exception as exc:
        print(f"ERROR: 迁移失败：{type(exc).__name__}（不输出连接串或凭据）")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
