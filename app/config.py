from decimal import Decimal
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Loads configuration from environment variables / .env file."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Phase 1: providers
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434"
    model_registry_path: str = "config/models.yaml"
    request_timeout_s: float = 60.0

    # Phase 2: persistence and budgets
    database_url: str = "postgresql+asyncpg://spend:spend_dev_password@127.0.0.1:5432/spend"
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_timeout_s: float = 0.5        # short: a down Redis should fail fast, not hang requests
    budget_warn_threshold: Decimal = Field(default=Decimal("0.8"), gt=0, lt=1)
    budget_fail_mode: Literal["open", "closed"] = "open"   # behaviour when Redis is down
    reconcile_on_startup: bool = True

    # Phase 3: routing
    routing_config_path: str = "config/routing.yaml"
    routing_profile: str = "dev"        # "dev" = mock model per tier, "production" = real providers

    # Phase 4: quality checks and escalation
    quality_config_path: str = "config/quality.yaml"
    verify_enabled: bool = True         # false = no sampling/verification (escalation still works)
    worker_concurrency: int = Field(default=4, ge=1, le=64)   # jobs processed in parallel
    worker_consumer_name: str | None = None   # unique per worker process; default host-pid

    # Phase 5: analytics API + dashboard
    analytics_cache_ttl_s: float = Field(default=30.0, ge=0)   # 0 disables the cache
    analytics_max_window_days: int = Field(default=366, ge=1)


settings = Settings()