"""应用配置。

输入：环境变量或 `.env`。
输出：不可变的 `Settings` 实例和启动期配置错误列表。
作用：集中管理模型、数据库、预算参数，避免在业务代码散落环境变量。
实现：使用 pydantic-settings 做类型转换，缺失凭据只报告不抛异常，便于 /healthz 明确返回。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """运行时配置。

    输入：环境变量（大小写不敏感）与可选 `.env`。
    输出：带默认值、经过类型校验的配置对象。
    作用：作为 API、图、存储和模型适配器共享的唯一配置来源。
    实现：字段名对应小写环境变量名，pydantic-settings 自动读取并校验。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "local"
    enable_test_api: bool = True

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    decision_mode: str = "tools"

    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    dashscope_embedding_model: str = "text-embedding-v3"
    embedding_dim: int = Field(default=1024, gt=0)

    database_url: str = ""

    uploads_dir: str = "uploads"
    max_upload_mb: int = Field(default=20, gt=0)
    max_pdf_pages: int = Field(default=200, gt=0)
    child_max_chars: int = Field(default=120, gt=0)
    parent_max_chars: int = Field(default=800, gt=0)

    rerank_enabled: bool = True
    rerank_model: str = "gte-rerank-v2"
    rerank_candidate_k: int = Field(default=30, ge=1)
    rerank_top_n: int = Field(default=8, ge=1)
    rerank_timeout_seconds: float = Field(default=20.0, gt=0)
    rerank_fallback: bool = True

    max_steps: int = Field(default=12, ge=1)
    max_tool_calls: int = Field(default=12, ge=1)
    max_revisions: int = Field(default=2, ge=0)
    max_rewrites: int = Field(default=2, ge=0)
    tool_timeout_seconds: float = Field(default=10.0, gt=0)
    recursion_limit: int = Field(default=24, ge=5)

    def config_errors(self, require_credentials: bool = True) -> list[str]:
        """返回阻止生产就绪的配置错误。

        输入：是否要求真实模型凭据。
        输出：人类可读错误字符串列表，空列表表示通过。
        作用：供 `/healthz`、`/readyz` 和启动日志复用。
        实现：只检查必填项和关键取值范围，不访问外部服务。
        """
        errors: list[str] = []
        if not self.database_url:
            errors.append("DATABASE_URL 未配置")
        if require_credentials:
            if not self.deepseek_api_key:
                errors.append("DEEPSEEK_API_KEY 未配置")
            if not self.dashscope_api_key:
                errors.append("DASHSCOPE_API_KEY 未配置")
        if self.embedding_dim != 1024:
            errors.append(f"EMBEDDING_DIM 必须为 1024，当前为 {self.embedding_dim}")
        if self.decision_mode not in {"tools", "json"}:
            errors.append(f"DECISION_MODE 仅支持 tools/json，当前为 {self.decision_mode}")
        return errors


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """读取并缓存全局配置。

    输入：无。
    输出：单例 `Settings`。
    作用：避免每个请求重复解析 `.env`。
    实现：`lru_cache` 缓存；测试可调用 `get_settings.cache_clear()` 重置。
    """
    return Settings()
