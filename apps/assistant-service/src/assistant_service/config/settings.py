"""Configuration via environment variables (Pydantic Settings)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class AppSettings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8093
    workers: int = 1
    debug: bool = False
    allow_anonymous: bool = True

    model_config = {"env_prefix": "ASSISTANT_APP__"}


class DatabaseSettings(BaseSettings):
    dsn: str = "postgresql://postgres:111111@localhost:5432/gateway"
    min_pool: int = 2
    max_pool: int = 20

    model_config = {"env_prefix": "ASSISTANT_DATABASE__"}


class RedisSettings(BaseSettings):
    url: str = "redis://localhost:6379/0"
    enabled: bool = True

    model_config = {"env_prefix": "ASSISTANT_REDIS__"}


class KBSettings(BaseSettings):
    url: str = "http://knowledge-service:8092"

    model_config = {"env_prefix": "ASSISTANT_KB__"}


class StorageSettings(BaseSettings):
    backend: str = "local"  # "s3" | "oss" | "local"
    local_path: str = "./uploads"
    s3_bucket: str = ""
    s3_region: str = "ap-southeast-2"
    s3_access_key: str = ""
    s3_secret_key: str = ""

    model_config = {"env_prefix": "ASSISTANT_STORAGE__"}


class LLMSettings(BaseSettings):
    """API keys for LLM providers — read from standard env vars."""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    deepseek_api_key: str = ""
    dashscope_api_key: str = ""
    gemini_api_key: str = ""
    tavily_api_key: str = ""

    model_config = {"env_prefix": ""}

    @classmethod
    def from_env(cls) -> LLMSettings:
        import os
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            dashscope_api_key=os.getenv("DASHSCOPE_API_KEY", ""),
            gemini_api_key=os.getenv("GEMINI_API_KEY", os.getenv("GOOGLE_API_KEY", "")),
            tavily_api_key=os.getenv("TAVILY_API_KEY", ""),
        )


class Settings(BaseSettings):
    app: AppSettings = Field(default_factory=AppSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    kb: KBSettings = Field(default_factory=KBSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings.from_env)


def get_settings() -> Settings:
    return Settings()
