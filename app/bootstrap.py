"""Composition root shared by the API (app/main.py) and the worker (app/worker.py).

Both processes need the same registry, routing policy, quality policy, adapters, database and
Redis, wired the same way. Building them here means the worker never imports the FastAPI app
(which would drag in HTTP routes it doesn't need) and the two can't drift apart.
"""
import logging
from dataclasses import dataclass

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.budgets import BudgetService, RedisBudgetStore
from app.config import Settings
from app.db import create_engine, create_session_factory
from app.providers import build_adapters
from app.providers.base import ProviderAdapter
from app.quality import QualityConfig, load_quality_config
from app.quality.queue import VerificationQueue
from app.registry import ModelRegistry
from app.routing import Router, RoutingConfig, available_providers, load_routing_config

logger = logging.getLogger("app")


def create_redis(url: str, timeout_s: float) -> Redis:
    # Short timeouts: when Redis is down we want a fast fail-open/closed decision,
    # not every request hanging for seconds
    return Redis.from_url(url, decode_responses=True, socket_timeout=timeout_s,
                          socket_connect_timeout=timeout_s)


@dataclass
class Core:
    settings: Settings
    registry: ModelRegistry
    routing_config: RoutingConfig
    providers: set[str]
    router: Router
    quality_config: QualityConfig
    http_client: httpx.AsyncClient
    adapters: dict[str, ProviderAdapter]
    engine: AsyncEngine
    redis: Redis
    session_factory: async_sessionmaker
    store: RedisBudgetStore
    budgets: BudgetService
    queue: VerificationQueue
    owns_engine: bool = True
    owns_redis: bool = True

    async def aclose(self) -> None:
        """Close only what this Core created (tests inject and close their own)."""
        await self.http_client.aclose()
        if self.owns_redis:
            await self.redis.aclose()
        if self.owns_engine:
            await self.engine.dispose()


def build_core(settings: Settings, *, engine: AsyncEngine | None = None,
               redis_client: Redis | None = None,
               redis_timeout_s: float | None = None) -> Core:
    # Config first and validated: a typo fails before any connection is opened (fail fast)
    registry = ModelRegistry(settings.model_registry_path)
    routing_config = load_routing_config(settings.routing_config_path,
                                         settings.routing_profile, registry)
    providers = available_providers(settings)
    router = Router(registry, routing_config, providers)
    quality_config = load_quality_config(settings.quality_config_path, registry,
                                         routing_config, providers, settings.verify_enabled)
    logger.info("Routing profile '%s' loaded; available providers: %s",
                routing_config.profile, ", ".join(sorted(providers)))
    logger.info("Quality config loaded: verification %s (reference model: %s, judge: %s)",
                "enabled" if quality_config.verify_enabled else "disabled",
                quality_config.verification.reference_model,
                quality_config.verification.judge.type)

    http_client = httpx.AsyncClient(timeout=settings.request_timeout_s)
    db_engine = engine or create_engine(settings.database_url)
    redis = redis_client or create_redis(settings.redis_url,
                                         redis_timeout_s or settings.redis_timeout_s)
    session_factory = create_session_factory(db_engine)
    store = RedisBudgetStore(redis)
    budgets = BudgetService(store, session_factory,
                            warn_threshold=settings.budget_warn_threshold,
                            fail_mode=settings.budget_fail_mode)
    return Core(settings=settings, registry=registry, routing_config=routing_config,
                providers=providers, router=router, quality_config=quality_config,
                http_client=http_client, adapters=build_adapters(http_client, settings),
                engine=db_engine, redis=redis, session_factory=session_factory, store=store,
                budgets=budgets,
                queue=VerificationQueue.from_config(redis, quality_config.verification),
                owns_engine=engine is None, owns_redis=redis_client is None)
