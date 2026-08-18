from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    orchestrator_model: str = "claude-opus-5"
    worker_model: str = "claude-haiku-4-5"
    orchestrator_effort: str = "medium"

    composio_api_key: str = ""
    composio_user_id: str = "research-agent"
    composio_toolkits: str = "COMPOSIO_SEARCH,EXA,FIRECRAWL,TAVILY"

    postgres_user: str = "research"
    postgres_password: str = "research"
    postgres_db: str = "research"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    max_tool_calls_per_lane: int = 3
    max_source_chars: int = 6000
    max_verify_retries: int = 1
    report_cache_ttl_hours: int = 168

    api_port: int = 8000
    cors_origins: str = "http://localhost:5173"

    @property
    def toolkit_list(self) -> list[str]:
        return [t.strip().upper() for t in self.composio_toolkits.split(",") if t.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def sqlalchemy_dsn(self) -> str:
        return self.dsn.replace("postgresql://", "postgresql+psycopg://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
